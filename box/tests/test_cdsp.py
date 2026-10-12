# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for k1000.cdsp — the CamillaDSP v5 websocket client.

Two layers:
* unit tests against an in-process fake engine (websockets.serve): reply matching, errors,
  timeouts, disconnect/reconnect, serialisation of concurrent callers, clamping;
* `engine` tests against a real camilladsp (SignalGenerator -> File /dev/null, never the ALSA
  card) on a private websocket port: every helper, audio following the faders, kill/restart
  reconnect with the on_connect re-apply, a frozen engine, and SetConfigValue latency.

No pytest-asyncio on the box (apt only), so each test drives its own asyncio.run().
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import shutil
import signal
import socket
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
import yaml
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from k1000.cdsp import (
    AUX1,
    AUX2,
    AUX3,
    MAIN,
    CamillaClient,
    CdspError,
    CdspNoConfig,
    CdspNotApplied,
    CdspNotRunning,
    CdspTimeout,
    CdspUnavailable,
    _matches,
    next_backoff,
)


def run(coro: Any, limit: float = 30.0) -> Any:
    return asyncio.run(asyncio.wait_for(coro, limit))


async def until(pred: Any, timeout: float = 3.0, what: str = "condition") -> None:
    """Poll a sync or async predicate; an engine still reconnecting counts as 'not yet'."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        try:
            ok = pred()
            if inspect.isawaitable(ok):
                ok = await ok
        except CdspUnavailable:
            ok = False
        if ok:
            return
        if loop.time() > deadline:
            raise AssertionError(f"{what} not met within {timeout} s")
        await asyncio.sleep(0.01)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ======================================================================= fake engine


class FakeEngine:
    """Stand-in for camilladsp's websocket server, speaking the v5 shapes proven on the box.

    Scripted commands: Fail (error result), Hang (never answers), Slow (answers after `delay`),
    Chatty (event + garbage + a stale reply before its own), Drop (closes the link).
    Unknown commands get the engine's real `{"reply": "Invalid", "error": ...}`.
    `states_after_setconfig`: the GetState answers after each SetConfig, in order (the last one
    sticks; "EXIT" closes the link, as camilladsp exits on a failed device open).
    `config = None` is wait mode (-w): GetConfigJson answers Ok with "null"; with
    `drop_config_on_setconfig` an upload is dropped (wait mode, device failed to open).
    """

    def __init__(self) -> None:
        self.log: list[dict[str, Any]] = []
        self.events: list[str] = []  # "rx" / "tx" in order: proves one request in flight
        self.faders = [{"volume": -40.0, "mute": True}] + [
            {"volume": 0.0, "mute": False} for _ in range(4)
        ]
        # Config values by pointer. `lag_reads` > 0 emulates CamillaDSP 5.0.0-beta.1: a write
        # is acknowledged at once but only becomes active after that many reads, and each write
        # is computed on the *active* config, so a second write in the window loses the first.
        self.active: dict[str, Any] = {}
        self.pending: list[Any] | None = None  # [new_active, reads_left]
        self.lag_reads = 0
        self.config: dict[str, Any] | None = {
            "filters": {"sub_delay": {"parameters": {"delay": 0.0}}}}
        self.state = "Running"
        self.state_script: list[str] = []
        self.states_after_setconfig: list[str] = []
        self.drop_config_on_setconfig = False
        self.stop_reason: Any = "None"
        self.latency_s = 0.0
        self.server: Any = None
        self.port = 0

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    async def start(self, port: int = 0) -> None:
        self.server = await serve(self._handler, "127.0.0.1", port, compression=None)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self.server.close()
        await self.server.wait_closed()

    async def _handler(self, ws: Any) -> None:
        try:
            async for raw in ws:
                msg = json.loads(raw)
                self.log.append(msg)
                self.events.append("rx")
                frames = await self._respond(ws, msg)
                for frame in frames:
                    await ws.send(frame if isinstance(frame, str) else json.dumps(frame))
                self.events.append("tx")
        except ConnectionClosed:
            pass

    async def _respond(self, ws: Any, msg: dict[str, Any]) -> list[Any]:
        name = msg["command"]
        if self.latency_s:
            await asyncio.sleep(self.latency_s)

        def ok(*value: Any) -> dict[str, Any]:
            reply: dict[str, Any] = {"reply": name, "result": "Ok"}
            if value:
                reply["value"] = value[0]
            return reply

        def err(result: str, message: str | None = None) -> dict[str, Any]:
            reply: dict[str, Any] = {"reply": name, "result": result}
            if message:
                reply["message"] = message
            return reply

        def fader() -> dict[str, Any] | None:
            idx = msg.get("fader")
            return self.faders[idx] if isinstance(idx, int) and 0 <= idx < 5 else None

        match name:
            case "GetVersion":
                return [ok("5.0.0-fake")]
            case "GetState":
                if self.state_script:
                    self.state = self.state_script.pop(0)
                if self.state == "EXIT":
                    ws.transport.abort()
                    return []
                return [ok(self.state)]
            case "GetStopReason":
                return [ok(self.stop_reason)]
            case "GetProcessingLoad":
                return [ok(0.5)]
            case "GetSignalLevels":
                return [ok({"playback_rms": [-23.0, -23.0], "playback_peak": [-20.0, -20.0],
                            "capture_rms": [-23.0, -23.0], "capture_peak": [-20.0, -20.0]})]
            case "GetClippedSamples":
                return [ok(7)]
            case "ResetClippedSamples":
                return [ok()]
            case "GetConfigValue":
                if self.pending is not None:
                    if self.pending[1] > 0:
                        self.pending[1] -= 1
                    else:
                        self.active, self.pending = self.pending[0], None
                return [ok(self.active.get(msg["value"], {"echo": msg["value"]}))]
            case "SetConfigValue":
                new = dict(self.active)  # stale base while a write is pending: the bug
                new[msg["pointer"]] = msg["value"]
                if self.lag_reads:
                    self.pending = [new, self.lag_reads]
                else:
                    self.active = new
                return [ok()]
            case "PatchConfig":
                if self.config is None:
                    return [err("InvalidRequestError", "No active config to modify")]
                _merge(self.config, msg["value"])
                return [ok()]
            case "SetConfig":
                try:
                    self.config = yaml.safe_load(msg["value"]) or {}
                except yaml.YAMLError as e:
                    return [err("ConfigReadError", str(e))]
                self.state_script = list(self.states_after_setconfig)
                if self.drop_config_on_setconfig:  # wait mode, device failed to open
                    self.config = None
                return [ok()]
            case "GetConfigJson":  # the engine sends a JSON string, "null" when it has none
                return [ok(json.dumps(self.config))]
            case "GetFaders":
                return [ok([dict(f) for f in self.faders])]
            case "GetFaderVolume":
                if (f := fader()) is None:
                    return [err("InvalidFaderError")]
                return [ok([msg["fader"], f["volume"]])]
            case "SetFaderVolume":
                if (f := fader()) is None:
                    return [err("InvalidFaderError")]
                f["volume"] = min(max(msg["value"], -150.0), 50.0)
                return [ok()]
            case "AdjustFaderVolume":
                if (f := fader()) is None:
                    return [err("InvalidFaderError")]
                lo, hi = msg.get("min", -150.0), msg.get("max", 50.0)
                f["volume"] = min(max(f["volume"] + msg["value"], lo), hi)
                return [ok([msg["fader"], f["volume"]])]
            case "SetFaderMute":
                if (f := fader()) is None:
                    return [err("InvalidFaderError")]
                f["mute"] = msg["value"]
                return [ok()]
            case "Fail":
                return [err("InvalidValueError", "too loud")]
            case "Hang":
                return []
            case "Slow":
                await asyncio.sleep(msg.get("delay", 0.3))
                return [ok(msg.get("tag"))]
            case "Chatty":
                return [
                    {"reply": "StateEvent", "result": "Ok", "value": {"state": "Running"}},
                    "this is not json",
                    "[1, 2, 3]",
                    {"reply": "GetVersion", "result": "Ok", "value": "stale"},
                    ok("mine"),
                ]
            case "Drop":
                ws.transport.abort()
                return []
            case _:
                return [{"reply": "Invalid", "error": f"unknown variant `{name}`"}]


def _merge(dst: dict[str, Any], patch: dict[str, Any]) -> None:
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        else:
            dst[k] = v


@contextlib.asynccontextmanager
async def rig(**client_kw: Any) -> Any:
    eng = FakeEngine()
    await eng.start()
    client = CamillaClient(eng.url, **client_kw)
    try:
        assert await client.connect(timeout=2.0)
        yield eng, client
    finally:
        await client.close()
        await eng.stop()


# ======================================================================= unit tests


def test_helpers_send_v5_shapes() -> None:
    async def body() -> None:
        async with rig() as (eng, c):
            assert await c.get_version() == "5.0.0-fake"
            assert await c.get_state() == "Running"
            assert await c.get_stop_reason() == "None"
            assert await c.get_processing_load() == 0.5
            levels = await c.get_signal_levels()
            assert set(levels) == {"playback_rms", "playback_peak", "capture_rms", "capture_peak"}
            assert await c.get_clipped_samples() == 7
            await c.reset_clipped_samples()

            p = "/filters/sub_trim/parameters/gain"
            assert await c.set_value(p, -3) == -3.0
            assert await c.get_value(p) == -3.0
            assert await c.set_value("/filters/sub_trim/parameters/inverted", True) is True
            patch = {"filters": {"sub_delay": {"parameters": {"delay": 1.5}}}}
            await c.patch_config(patch)
            assert (await c.get_config_json())["filters"]["sub_delay"]["parameters"]["delay"] == 1.5
            await c.set_config_yaml("title: x\n")
            assert await c.get_config_json() == {"title": "x"}

            assert (await c.get_faders())[0] == {"volume": -40.0, "mute": True}
            assert await c.set_fader(AUX1, -6) == -6.0
            assert await c.adjust_fader(AUX1, 3, max=-4) == -4.0
            await c.set_fader_mute(AUX1, True)
            assert (await c.get_faders())[AUX1] == {"volume": -4.0, "mute": True}

            sent = {m["command"]: m for m in eng.log}  # last of each kind
            inv = "/filters/sub_trim/parameters/inverted"
            assert sent["SetConfigValue"] == {"command": "SetConfigValue", "pointer": inv,
                                              "value": True}
            assert sent["GetConfigValue"] == {"command": "GetConfigValue", "value": inv}
            assert sent["PatchConfig"] == {"command": "PatchConfig", "value": patch}
            # every config write is followed by its confirming read-back
            names = [m["command"] for m in eng.log]
            for write, read in (("SetConfigValue", "GetConfigValue"),
                                ("PatchConfig", "GetConfigJson"), ("SetConfig", "GetConfigJson")):
                i = names.index(write)
                assert names[i + 1] == read, (write, names[i:i + 3])
            assert sent["SetConfig"] == {"command": "SetConfig", "value": "title: x\n"}
            assert sent["SetFaderVolume"] == {"command": "SetFaderVolume", "fader": 1, "value": -6.0}
            assert sent["AdjustFaderVolume"] == {"command": "AdjustFaderVolume", "fader": 1,
                                                 "value": 3.0, "min": -150.0, "max": -4.0}
            assert sent["SetFaderMute"] == {"command": "SetFaderMute", "fader": 1, "value": True}
            assert sent["GetFaders"] == {"command": "GetFaders"}

    run(body())


def test_error_replies_raise_and_keep_the_link() -> None:
    async def body() -> None:
        async with rig() as (eng, c):
            with pytest.raises(CdspError) as ei:
                await c.command("Fail")
            assert (ei.value.command, ei.value.result, ei.value.message) == (
                "Fail", "InvalidValueError", "too loud")
            with pytest.raises(CdspError) as ei:
                await c.command("SetFaderVolume", fader=9, value=0.0)
            assert ei.value.result == "InvalidFaderError" and ei.value.message is None
            with pytest.raises(CdspError) as ei:
                await c.command("Bogus")
            assert ei.value.result == "Invalid" and "unknown variant" in ei.value.message
            with pytest.raises(CdspError) as ei:  # the engine, not PyYAML, judges a config
                await c.set_config_yaml("garbage: [")
            assert ei.value.result == "ConfigReadError"
            assert not isinstance(ei.value, CdspUnavailable)
            # an error reply is a healthy link: no reconnect, next command fine
            assert await c.get_version() == "5.0.0-fake"
            assert c.connect_count == 1

    run(body())


def test_unsolicited_and_malformed_frames_are_skipped() -> None:
    async def body() -> None:
        async with rig() as (eng, c):
            assert await c.command("Chatty") == "mine"
            assert await c.get_version() == "5.0.0-fake"
            assert c.connect_count == 1

    run(body())


def test_timeout_resets_link_and_reconnects() -> None:
    async def body() -> None:
        ups: list[int] = []

        async def on_connect(cl: CamillaClient) -> None:
            ups.append(cl.connect_count)

        async with rig(timeout=0.3, backoff_min=0.05, on_connect=on_connect) as (eng, c):
            await until(lambda: ups == [1])
            t0 = time.perf_counter()
            with pytest.raises(CdspTimeout) as ei:
                await c.command("Hang")
            assert 0.25 < time.perf_counter() - t0 < 1.0
            assert isinstance(ei.value, CdspUnavailable)  # callers catching Unavailable get both
            await until(lambda: ups == [1, 2], what="reconnect + on_connect after timeout")
            assert await c.get_version() == "5.0.0-fake"
            # per-call override for a command known to be slow
            assert await c.command("Slow", delay=0.5, tag="late", timeout=2.0) == "late"
            assert c.connect_count == 2

    run(body())


def test_cancelled_request_cannot_desync_replies() -> None:
    async def body() -> None:
        async with rig(backoff_min=0.05) as (eng, c):
            task = asyncio.create_task(c.command("Slow", delay=0.3, tag="old"))
            await asyncio.sleep(0.1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await until(lambda: c.connect_count == 2, what="link reset after cancel")
            # without the reset, this would receive the stale "old" reply
            assert await c.command("Slow", delay=0.0, tag="new") == "new"

    run(body())


def test_disconnect_reconnect_and_hooks() -> None:
    async def body() -> None:
        ups: list[int] = []
        downs: list[int] = []

        async def on_connect(cl: CamillaClient) -> None:
            ups.append(cl.connect_count)

        async def on_disconnect(cl: CamillaClient) -> None:
            downs.append(cl.connect_count)

        eng = FakeEngine()
        await eng.start()
        port = eng.port
        c = CamillaClient(eng.url, on_connect=on_connect, on_disconnect=on_disconnect,
                          backoff_min=0.05, backoff_max=0.2)
        try:
            assert await c.connect(timeout=2.0)
            await until(lambda: ups == [1])
            await eng.stop()
            await until(lambda: not c.connected, what="disconnect detected")
            await until(lambda: downs == [1])
            t0 = time.perf_counter()
            with pytest.raises(CdspUnavailable):
                await c.get_version()
            assert time.perf_counter() - t0 < 0.1  # fails fast, never blocks the UI

            eng2 = FakeEngine()
            await eng2.start(port)
            try:
                await until(lambda: ups == [1, 2], what="reconnect + on_connect")
                assert await c.get_version() == "5.0.0-fake"
                # link lost while a request is in flight: Unavailable, fast, not a timeout
                t0 = time.perf_counter()
                with pytest.raises(CdspUnavailable) as ei:
                    await c.command("Drop")
                assert not isinstance(ei.value, CdspTimeout)
                assert time.perf_counter() - t0 < 0.5
                await until(lambda: ups == [1, 2, 3])
                assert downs == [1, 2]
            finally:
                await c.close()
                await eng2.stop()
        finally:
            await c.close()

    run(body())


def test_engine_absent_at_start_then_appears() -> None:
    async def body() -> None:
        port = free_port()
        c = CamillaClient(f"ws://127.0.0.1:{port}", backoff_min=0.05, backoff_max=0.2)
        eng = FakeEngine()
        try:
            assert await c.connect(timeout=0.3) is False  # no raise: boot shows ENGINE DOWN
            with pytest.raises(CdspUnavailable):
                await c.get_state()
            await eng.start(port)
            assert await c.wait_connected(2.0)
            assert await c.get_state() == "Running"
        finally:
            await c.close()
            if eng.server is not None:
                await eng.stop()

    run(body())


def test_backoff_is_capped() -> None:
    d, seq = 0.1, []
    for _ in range(7):
        seq.append(d)
        d = next_backoff(d, 2.0)
    assert seq == [0.1, 0.2, 0.4, 0.8, 1.6, 2.0, 2.0]

    async def body() -> int:
        attempts = 0

        async def refuse(reader: Any, writer: Any) -> None:  # TCP accepts, handshake fails
            nonlocal attempts
            attempts += 1
            writer.close()

        srv = await asyncio.start_server(refuse, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        c = CamillaClient(f"ws://127.0.0.1:{port}", backoff_min=0.05, backoff_max=0.2)
        try:
            assert await c.connect(timeout=1.0) is False
        finally:
            await c.close()
            srv.close()
            await srv.wait_closed()
        return attempts

    # retries at ~0, .05, .15, .35, .55, .75, .95 s: steady at the cap, not a hot loop
    assert 4 <= run(body()) <= 10


def test_concurrent_callers_are_serialised() -> None:
    async def body() -> None:
        async with rig() as (eng, c):
            eng.latency_s = 0.003
            ptrs = [f"/filters/f{i}/parameters/gain" for i in range(40)]
            calls = [c.get_value(p) for p in ptrs]
            calls.insert(20, c.command("Fail"))
            results = await asyncio.gather(*calls, return_exceptions=True)
            assert isinstance(results.pop(20), CdspError)
            assert results == [{"echo": p} for p in ptrs]  # every caller got its own reply
            assert eng.events == ["rx", "tx"] * 41  # strictly one request in flight
            assert c.connect_count == 1

    run(body())


def test_clamping_and_refusals() -> None:
    async def body() -> None:
        async with rig(fader_max_db=6.0) as (eng, c):
            assert await c.set_fader(MAIN, 99) == 6.0
            assert await c.set_fader(AUX2, -999) == -150.0
            for bad in (float("nan"), float("inf"), "loud", True, None):
                with pytest.raises(ValueError):
                    await c.set_fader(AUX1, bad)
            for bad_idx in (-1, 5, 1.0, True):
                with pytest.raises(ValueError):
                    await c.set_fader(bad_idx, 0.0)
                with pytest.raises(ValueError):
                    await c.set_fader_mute(bad_idx, True)
            assert await c.adjust_fader(AUX1, +50) == 6.0  # client ceiling even without max=
            assert await c.adjust_fader(AUX1, -3, min=-200, max=100) == 3.0
            assert (eng.log[-1]["min"], eng.log[-1]["max"]) == (-150.0, 6.0)
            with pytest.raises(ValueError):
                await c.adjust_fader(AUX1, 1, min=0, max=-1)
            with pytest.raises(ValueError):
                await c.adjust_fader(AUX1, float("nan"))
            assert await c.set_value("/filters/x/parameters/gain", 30.0, clamp=(-12, 12)) == 12.0
            with pytest.raises(ValueError):
                await c.set_value("/filters/x/parameters/gain", float("nan"))
            with pytest.raises(ValueError):
                await c.set_value("filters/x", 1.0)  # not a JSON Pointer
            with pytest.raises(ValueError):
                await c.patch_config({"filters": {"x": {"parameters": {"gain": float("inf")}}}})
            sent = [m["command"] for m in eng.log]
            # nothing invalid reached the engine
            assert sent == ["SetFaderVolume", "SetFaderVolume", "AdjustFaderVolume",
                            "AdjustFaderVolume", "SetConfigValue", "GetConfigValue"]
        with pytest.raises(ValueError):
            CamillaClient(fader_max_db=60.0)

    run(body())


def test_config_writes_survive_the_v5_stale_base_bug() -> None:
    """The fake reproduces 5.0.0-beta.1's lost update with raw commands; the client's confirmed
    writes must not lose any."""
    a, b = "/filters/sub_trim/parameters/gain", "/filters/sub_delay/parameters/delay"

    async def body() -> None:
        async with rig() as (eng, c):
            eng.lag_reads = 2
            # control: raw back-to-back writes lose the first one, as on the real engine
            await c.command("SetConfigValue", pointer=a, value=-3.0)
            await c.command("SetConfigValue", pointer=b, value=1.25)
            for _ in range(3):
                await c.command("GetConfigValue", value=a)  # let the pending write land
            assert eng.active.get(a) is None and eng.active[b] == 1.25  # -3.0 was lost

            # the client: each write waits until visible, so nothing is lost
            eng.log.clear()
            assert await c.set_value(a, -6.0) == -6.0
            assert await c.set_value(b, 2.5) == 2.5
            assert (eng.active[a], eng.active[b]) == (-6.0, 2.5)
            names = [m["command"] for m in eng.log]
            assert names == ["SetConfigValue"] + ["GetConfigValue"] * 3 + \
                            ["SetConfigValue"] + ["GetConfigValue"] * 3

            # nobody can slip in between a write and its confirmation
            eng.log.clear()
            await asyncio.gather(c.set_value(a, -9.0), c.get_faders(), c.set_value(b, 3.0))
            names = [m["command"] for m in eng.log]
            first = names.index("SetConfigValue")
            assert names[first:first + 4] == ["SetConfigValue"] + ["GetConfigValue"] * 3
            assert (eng.active[a], eng.active[b]) == (-9.0, 3.0)
            assert c.connect_count == 1

    run(body())


def test_write_never_visible_raises_not_applied() -> None:
    async def body() -> None:
        async with rig(confirm_timeout=0.1) as (eng, c):
            eng.lag_reads = 10**6  # e.g. another writer keeps overwriting, or engine not applying
            t0 = time.perf_counter()
            with pytest.raises(CdspNotApplied) as ei:
                await c.set_value("/filters/sub_trim/parameters/gain", -3.0)
            assert 0.09 < time.perf_counter() - t0 < 0.5
            assert isinstance(ei.value, CdspError)
            assert (ei.value.command, ei.value.result) == ("SetConfigValue", "NotApplied")
            assert c.connected and c.connect_count == 1  # a healthy link stays up
            eng.lag_reads, eng.pending = 0, None
            assert await c.set_value("/filters/sub_trim/parameters/gain", -3.0) == -3.0

    run(body())


def test_adjust_fader_never_lifts_a_fader_below_its_floor() -> None:
    """The engine clamps AdjustFaderVolume's result into [min, max]: sent as-is, a +0.5 dB
    detent on a -150 dB fader with min=-6 lands at -6 dB (+144 dB, seen on the box). Whatever
    the start, bounds and sign: never up by more than delta, never up on a turn-down."""

    async def body() -> None:
        async with rig() as (eng, c):
            def put(idx: int, db: float) -> None:
                eng.faders[idx]["volume"] = db

            put(AUX1, -150.0)
            assert await c.adjust_fader(AUX1, +0.5, min=-6, max=6) == -149.5
            assert [m["command"] for m in eng.log[-2:]] == ["GetFaderVolume", "AdjustFaderVolume"]
            assert eng.log[-1]["min"] == -150.0  # floor lowered to where the fader is
            put(MAIN, -60.0)
            assert await c.adjust_fader(MAIN, -0.5, min=-40, max=0) == -60.5  # down stays down
            put(MAIN, -30.0)
            assert await c.adjust_fader(MAIN, -20, min=-40, max=0) == -40.0  # floor on the way down
            put(AUX2, 10.0)
            assert await c.adjust_fader(AUX2, +0.5, min=-6, max=6) == 6.0  # ceiling pulls down
            put(AUX3, -149.9)
            assert await c.adjust_fader(AUX3, -0.5, min=-6) == -150.0
            assert eng.log[-1]["min"] == -150.0  # the engine would honour min=-150.4
            # at the engine floor nothing can sit below min: one round trip, no read
            assert await c.adjust_fader(AUX1, 1.0) == -148.5
            assert eng.log[-2]["command"] != "GetFaderVolume"

            for start in (-150.0, -100.0, -60.0, -40.0, -6.5, -6.0, 0.0, 5.0, 6.0, 10.0, 40.0):
                for delta in (-20.0, -0.5, 0.0, 0.5, 20.0):
                    for lo, hi in ((-40.0, 0.0), (-6.0, 6.0), (-150.0, 12.0), (-80.0, -20.0)):
                        put(AUX1, start)
                        got = await c.adjust_fader(AUX1, delta, min=lo, max=hi)
                        case = (start, delta, lo, hi, got)
                        assert got <= max(start + delta, start), case  # up by delta at most
                        if delta <= 0:
                            assert got <= start, case  # a turn-down never turns up
                        floor = lo if start >= lo else -150.0
                        assert got == min(max(start + delta, floor), hi), case

        async with rig(fader_min_db=-80.0) as (eng, c):  # a client floor applies the same rule
            eng.faders[AUX1]["volume"] = -150.0
            assert await c.adjust_fader(AUX1, +0.5) == -149.5

    run(body())


def test_gain_ceiling_is_applied_to_every_gain_write() -> None:
    async def body() -> None:
        async with rig() as (eng, c):  # defaults: gains within [-150, +12] dB
            g = "/filters/top_trim/parameters/gain"
            assert await c.set_value(g, 30.0) == 12.0
            assert eng.log[-2] == {"command": "SetConfigValue", "pointer": g, "value": 12.0}
            assert await c.set_value(g, -999.0) == -150.0
            assert await c.set_value(g, 30.0, clamp=(-40, 40)) == 12.0  # the ceiling wins
            assert await c.set_value(g, 3.0, clamp=(-6, 0)) == 0.0  # a tighter clamp too
            with pytest.raises(ValueError):
                await c.set_value(g, 1.0, clamp=(6, -6))
            src = "/mixers/split/mapping/0/sources/0/gain"
            got = await c.set_value(src, 20)
            assert got == 12 and type(got) is int
            peq = {"type": "Peaking", "freq": 30000.0, "gain": 30.0, "q": 1.0}
            assert await c.set_value("/filters/live_peq_1/parameters", peq) == {
                "type": "Peaking", "freq": 30000.0, "gain": 12.0, "q": 1.0}  # only gains
            assert peq["gain"] == 30.0  # the caller's dict is not mutated
            await c.patch_config({
                "filters": {"a": {"parameters": {"gain": 30.0, "freq": 15000}}},
                "mixers": {"m": {"mapping": [{"dest": 0, "sources": [
                    {"channel": 0, "gain": -200.0}]}]}}})
            sent = [m for m in eng.log if m["command"] == "PatchConfig"][-1]["value"]
            assert sent["filters"]["a"]["parameters"] == {"gain": 12.0, "freq": 15000}
            assert sent["mixers"]["m"]["mapping"][0]["sources"][0]["gain"] == -150.0
            # a whole config is refused, not rewritten: nothing reaches the engine
            n = len(eng.log)
            with pytest.raises(ValueError, match="gain"):
                await c.set_config_yaml("filters:\n  t: {type: Gain, parameters: {gain: 30.0}}\n")
            with pytest.raises(ValueError):
                await c.set_config_yaml(
                    "mixers:\n  m: {mapping: [{dest: 0, sources: [{channel: 0, gain: .nan}]}]}\n")
            assert len(eng.log) == n
        async with rig(gain_max_db=6.0) as (eng, c):
            assert await c.set_value("/filters/x/parameters/gain", 9.5) == 6.0
        for bad in ({"gain_max_db": 200.0}, {"gain_min_db": 0.0, "gain_max_db": 0.0}):
            with pytest.raises(ValueError):
                CamillaClient(**bad)

    run(body())


def test_set_value_keeps_integer_fields_integer() -> None:
    """The engine rejects 256.0 for a usize field (target_level, chunksize): an int stays an
    int on the wire, including after a clamp."""

    async def body() -> None:
        async with rig() as (eng, c):
            tl = "/devices/target_level"
            got = await c.set_value(tl, 256)
            assert got == 256 and type(got) is int
            assert eng.log[-2] == {"command": "SetConfigValue", "pointer": tl, "value": 256}
            assert type(eng.log[-2]["value"]) is int  # json "256", not "256.0"
            got = await c.set_value(tl, 1000, clamp=(0.5, 512.5))
            assert got == 512 and type(got) is int
            got = await c.set_value(tl, 0, clamp=(0.5, 512.5))
            assert got == 1 and type(got) is int
            got = await c.set_value("/filters/x/parameters/gain", -3)  # inside the ceiling
            assert got == -3 and type(got) is int
            assert await c.set_value("/filters/x/parameters/delay", 1.5) == 1.5

    run(body())


def test_no_active_config_is_explicit() -> None:
    """Wait mode (-w): GetConfigJson answers Ok + null. A poller doing cfg["filters"] would die
    on a TypeError; the client raises a CdspError subclass instead."""

    async def body() -> None:
        async with rig(settle_s=0.05) as (eng, c):
            eng.config, eng.state = None, "Inactive"
            with pytest.raises(CdspNoConfig) as ei:
                await c.get_config_json()
            assert isinstance(ei.value, CdspError) and ei.value.result == "NoActiveConfig"
            with pytest.raises(CdspError) as ei:
                await c.patch_config({"filters": {}})
            assert ei.value.result == "InvalidRequestError"
            # first upload whose device fails: the null read is not mistaken for "stale"
            eng.states_after_setconfig, eng.drop_config_on_setconfig = ["Inactive"], True
            eng.stop_reason = {"CaptureError": "device not found"}
            t0 = time.perf_counter()
            with pytest.raises(CdspNotRunning) as ei:
                await c.set_config_yaml("devices: {samplerate: 44100}\n")
            assert time.perf_counter() - t0 < 1.0 and "CaptureError" in ei.value.message
            eng.drop_config_on_setconfig = False
            eng.states_after_setconfig = ["Inactive", "Starting", "Running"]
            await c.set_config_yaml("devices: {samplerate: 48000}\n")  # waits until Running
            assert eng.state == "Running" and not eng.state_script
            assert (await c.get_config_json())["devices"] == {"samplerate": 48000}
            assert c.connect_count == 1

    run(body())


def test_set_config_yaml_waits_for_the_engine_on_new_devices() -> None:
    """The engine reports a new config (and even Running) before it opens a new device; a
    failed open then stops it (wait mode) or kills it (file mode). Only a steady Running counts."""

    def cfg(chunksize: int = 256, gain: float = 0.0, filename: str = "/dev/null") -> str:
        return (f"devices: {{samplerate: 48000, chunksize: {chunksize}, "
                f"playback: {{type: File, channels: 2, filename: {filename}}}}}\n"
                f"filters: {{t: {{type: Gain, parameters: {{gain: {gain}}}}}}}\n")

    def gets_after_setconfig(eng: FakeEngine) -> int:
        names = [m["command"] for m in eng.log]
        return names[names.index("SetConfig"):].count("GetState")

    async def body() -> None:
        async with rig(settle_s=0.1, backoff_min=0.05) as (eng, c):
            await c.set_config_yaml(cfg())

            eng.log.clear()  # filter-only change: no device reopen, no state watch
            await c.set_config_yaml(cfg(gain=-3.0))
            assert [m["command"] for m in eng.log] == [
                "GetConfigJson", "GetState", "SetConfig", "GetConfigJson"]

            eng.log.clear()  # device change: stale Running, restart, then steady Running
            eng.states_after_setconfig = ["Running", "Starting", "Starting", "Running"]
            t0 = time.perf_counter()
            await c.set_config_yaml(cfg(chunksize=512))
            assert time.perf_counter() - t0 >= 0.1
            assert gets_after_setconfig(eng) >= 4

            # the device does not open (wait mode): config dropped, Inactive, a stop reason
            eng.states_after_setconfig = ["Running", "Inactive"]
            eng.drop_config_on_setconfig = True
            eng.stop_reason = {"PlaybackError": "No such file or directory (os error 2)"}
            t0 = time.perf_counter()
            with pytest.raises(CdspNotRunning) as ei:
                await c.set_config_yaml(cfg(filename="/nonexistent/out.raw"))
            assert time.perf_counter() - t0 < 1.0  # settle_s, not the 10 s timeout
            assert isinstance(ei.value, CdspError) and ei.value.result == "NotRunning"
            assert "Inactive" in ei.value.message and "PlaybackError" in ei.value.message
            assert c.connected  # the link is fine, the engine is not

            # never settles (Stalled): NotRunning at the timeout
            eng.drop_config_on_setconfig = False
            eng.state, eng.stop_reason = "Running", "None"
            eng.states_after_setconfig = ["Stalled"]
            t0 = time.perf_counter()
            with pytest.raises(CdspNotRunning) as ei:
                await c.set_config_yaml(cfg(chunksize=1024), timeout=0.4)
            assert 0.35 < time.perf_counter() - t0 < 1.5 and "Stalled" in ei.value.message

            # same content re-uploaded to a stopped engine (a retry): still checked
            eng.log.clear()
            eng.state, eng.states_after_setconfig = "Inactive", ["Starting", "Running"]
            await c.set_config_yaml(cfg(chunksize=1024))
            assert gets_after_setconfig(eng) >= 2 and eng.state == "Running"

            # started from a file, the engine exits on a failed open: CdspUnavailable
            eng.states_after_setconfig = ["Running", "EXIT"]
            with pytest.raises(CdspUnavailable):
                await c.set_config_yaml(cfg(chunksize=2048))

    run(body())


def test_matches_semantics() -> None:
    assert _matches(8834.0, 8834) and _matches(-3, -3.0)
    assert not _matches(1, True) and not _matches(True, 1) and _matches(True, True)
    assert _matches({"delay": 1.5, "delay_unit": "ms", "subsample": None}, {"delay": 1.5})
    assert not _matches({"delay": 1.0}, {"delay": 1.5})
    assert _matches({"a": 1}, {"a": 1, "b": None})  # None = engine default, not checked
    assert _matches([{"x": 1, "y": None}], [{"x": 1}]) and not _matches([1], [1, 2])
    assert _matches("Peaking", "Peaking") and not _matches("Lowpass", "Peaking")


def test_hook_failure_is_contained_and_close_is_clean() -> None:
    async def body() -> None:
        async def broken(cl: CamillaClient) -> None:
            raise RuntimeError("controller bug")

        async with rig(on_connect=broken) as (eng, c):
            await asyncio.sleep(0.05)
            assert await c.get_version() == "5.0.0-fake"
        assert not c.connected
        with pytest.raises(CdspUnavailable):
            await c.get_version()
        await c.close()  # idempotent
        left = [t for t in asyncio.all_tasks() if t.get_name().startswith("cdsp-") and not t.done()]
        assert left == []

    run(body())


# ======================================================================= engine tests

CDSP_BIN = shutil.which("camilladsp") or "/usr/local/bin/camilladsp"
ENGINE_PORT = int(os.environ.get("K1000_CDSP_TEST_PORT", "12342"))

# Small v5 config in the shape of the v0 pipeline: generator -> 2->4 split -> sub/top chains.
ENGINE_YAML = """\
title: k1000 cdsp engine test
devices:
  samplerate: 48000
  chunksize: 256
  volume_ramp_time_ms: 300
  capture:
    type: SignalGenerator
    channels: 2
    signal: {type: Sine, freq: 1000, level: -20.0}
  playback:
    type: File
    channels: 4
    filename: /dev/null
    format: S32_LE
mixers:
  split:
    channels: {in: 2, out: 4}
    mapping:
      - {dest: 0, sources: [{channel: 0, gain: 0}]}
      - {dest: 1, sources: [{channel: 1, gain: 0}]}
      - {dest: 2, sources: [{channel: 0, gain: 0}]}
      - {dest: 3, sources: [{channel: 1, gain: 0}]}
filters:
  live_peq_1: {type: Biquad, parameters: {type: Peaking, freq: 1000, gain: 0.0, q: 1.0}}
  sub_trim: {type: Gain, parameters: {gain: 0.0, inverted: false}}
  sub_lowpass: {type: Biquad, parameters: {type: Lowpass, freq: SUBLP, q: 0.707}}
  sub_delay: {type: Delay, parameters: {delay: 0.0, delay_unit: ms}}
  sub_fader: {type: Volume, parameters: {fader: Aux1, ramp_time_ms: 300}}
  top_trim: {type: Gain, parameters: {gain: 0.0, inverted: false}}
  top_lowcut: {type: Biquad, parameters: {type: Highpass, freq: 10, q: 0.707}}
  top_delay: {type: Delay, parameters: {delay: 0.0, delay_unit: ms}}
  top_fader: {type: Volume, parameters: {fader: Aux2, ramp_time_ms: 300}}
pipeline:
  - {type: Filter, channels: [0, 1], names: [live_peq_1]}
  - {type: Mixer, name: split}
  - {type: Filter, channels: [0, 1], names: [sub_trim, sub_lowpass, sub_delay, sub_fader]}
  - {type: Filter, channels: [2, 3], names: [top_trim, top_lowcut, top_delay, top_fader]}
"""


def engine_yaml(sub_lowpass_hz: float = 20000) -> str:
    return ENGINE_YAML.replace("SUBLP", str(sub_lowpass_hz))


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


class Engine:
    """A private camilladsp, started like the production unit (-m -g-40: boots muted), from
    a config file, or in wait mode (-w, no config) with ``wait_mode=True``."""

    def __init__(self, workdir: Path, port: int, wait_mode: bool = False) -> None:
        self.port = port
        self.cfg = workdir / "engine.yml"
        self.cfg.write_text(engine_yaml())
        self.logfile = workdir / "camilladsp.log"
        self.wait_mode = wait_mode
        self.proc: subprocess.Popen[bytes] | None = None

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    def start(self) -> None:
        if _port_open(self.port):
            raise RuntimeError(f"port {self.port} already in use: a stale camilladsp?")
        log = open(self.logfile, "ab")
        self.proc = subprocess.Popen(
            [CDSP_BIN, "-p", str(self.port), "-a", "127.0.0.1", "-m", "-g-40",
             "-w" if self.wait_mode else str(self.cfg)],
            stdout=log, stderr=subprocess.STDOUT)
        log.close()
        deadline = time.monotonic() + 5.0
        while not _port_open(self.port):
            if self.proc.poll() is not None:
                raise RuntimeError("camilladsp exited:\n" + self.logfile.read_text()[-2000:])
            if time.monotonic() > deadline:
                raise RuntimeError("camilladsp websocket did not open in 5 s")
            time.sleep(0.02)

    def signal(self, sig: int) -> None:
        assert self.proc is not None
        os.kill(self.proc.pid, sig)

    def freeze(self) -> None:
        """SIGSTOP and wait until *every* thread is stopped: kill() returns before the group
        stop completes, and a request sent in that window can still be answered (seen 1/12)."""
        self.signal(signal.SIGSTOP)
        tasks = Path(f"/proc/{self.proc.pid}/task")  # type: ignore[union-attr]
        deadline = time.monotonic() + 2.0
        while True:
            states = [(t / "status").read_text().split("State:")[1].split()[0]
                      for t in tasks.iterdir()]
            if all(s == "T" for s in states):
                return
            if time.monotonic() > deadline:
                raise RuntimeError(f"camilladsp threads not stopped: {states}")
            time.sleep(0.005)

    def kill(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGCONT)
            self.proc.kill()
            self.proc.wait(5)
        self.proc = None


@pytest.fixture
def engine(tmp_path: Path) -> Any:
    eng = Engine(tmp_path, ENGINE_PORT)
    eng.start()
    try:
        yield eng
    finally:
        eng.kill()


async def _running(c: CamillaClient) -> None:
    await until(lambda: _is_running(c), timeout=3.0, what="engine Running")


async def _is_running(c: CamillaClient) -> bool:
    return await c.get_state() == "Running"


@pytest.mark.engine
@pytest.mark.skipif(not os.access(CDSP_BIN, os.X_OK), reason="no camilladsp binary")
def test_engine_every_helper(engine: Engine) -> None:
    async def body() -> None:
        async with CamillaClient(engine.url) as c:
            assert c.connected
            assert (await c.get_version()).startswith("5.")
            await _running(c)
            assert await c.get_stop_reason() == "None"
            faders = await c.get_faders()
            assert len(faders) == 5
            assert faders[MAIN] == {"volume": -40.0, "mute": True}  # -m -g-40: boots muted
            assert 0.0 <= await c.get_processing_load() < 50.0
            levels = await c.get_signal_levels()
            assert abs(max(levels["capture_peak"]) + 20.0) < 0.5  # the -20 dB generator
            assert max(levels["playback_peak"]) < -100.0  # Main muted -> silence out
            assert await c.get_clipped_samples() == 0

            gain = "/filters/sub_trim/parameters/gain"
            assert await c.set_value(gain, -3.5) == -3.5
            assert await c.get_value(gain) == -3.5
            await c.set_value("/filters/sub_trim/parameters/inverted", True)
            assert await c.get_value("/filters/sub_trim/parameters/inverted") is True
            await c.set_value("/filters/sub_delay/parameters/delay", 2.25)
            await c.patch_config(
                {"filters": {"live_peq_1": {"parameters": {"freq": 250.0, "gain": -6.0}}}})
            peq = await c.get_value("/filters/live_peq_1/parameters")
            assert (peq["freq"], peq["gain"], peq["type"]) == (250.0, -6.0, "Peaking")
            cfg = await c.get_config_json()
            assert cfg["filters"]["sub_delay"]["parameters"]["delay"] == 2.25
            assert cfg["filters"]["sub_delay"]["parameters"]["delay_unit"] == "ms"
            assert cfg["filters"]["sub_fader"]["parameters"]["fader"] == "Aux1"
            assert [s.get("name") for s in cfg["pipeline"]][1] == "split"

            assert not _matches(cfg, yaml.safe_load(engine_yaml()))  # edits are visible...
            await c.set_value(gain, 0.0)
            await c.set_value("/filters/sub_trim/parameters/inverted", False)
            await c.set_value("/filters/sub_delay/parameters/delay", 0.0)
            await c.patch_config(
                {"filters": {"live_peq_1": {"parameters": {"freq": 1000, "gain": 0.0}}}})
            # ...and once undone, our config shapes read back without normalisation surprises
            # (set_config_yaml's confirmation relies on it)
            assert _matches(await c.get_config_json(), yaml.safe_load(engine_yaml()))
            await c.set_config_yaml(engine_yaml())  # identical content: no confirm wait
            t = time.perf_counter()
            await c.set_config_yaml(engine_yaml(sub_lowpass_hz=80))
            print(f"\n[cdsp] SetConfig (same devices) confirmed in "
                  f"{(time.perf_counter() - t) * 1e3:.1f} ms")
            assert await c.get_value("/filters/sub_lowpass/parameters/freq") == 80
            await _running(c)

            assert await c.set_fader(AUX1, -6) == -6.0
            assert await c.adjust_fader(AUX1, +3, max=-4) == -4.0
            assert await c.adjust_fader(AUX1, -1) == -5.0
            await c.set_fader_mute(AUX1, True)
            assert (await c.get_faders())[AUX1] == {"volume": -5.0, "mute": True}
            await c.set_fader_mute(AUX1, False)
            assert await c.set_fader(MAIN, -20) == -20.0
            await c.set_fader_mute(MAIN, False)
            assert (await c.get_faders())[MAIN] == {"volume": -20.0, "mute": False}
            assert await c.set_fader(MAIN, 99) == c.fader_max_db  # client ceiling, not +50
            assert (await c.get_faders())[MAIN]["volume"] == c.fader_max_db

            with pytest.raises(CdspError) as ei:
                await c.set_value("/filters/nope/parameters/gain", 1.0)
            assert ei.value.result == "InvalidRequestError"
            with pytest.raises(CdspError) as ei:  # the engine's own limit, raw (unconfirmed)
                await c.command("SetConfigValue", pointer=gain, value=999.0)
            assert ei.value.result == "ConfigValidationError" and "150" in ei.value.message
            assert await c.set_value(gain, 999.0) == c.gain_max_db  # the client never sends it
            assert await c.get_value(gain) == c.gain_max_db
            await c.set_value(gain, 0.0)
            with pytest.raises(CdspError) as ei:
                await c.command("SetFaderVolume", fader=9, value=0.0)
            assert ei.value.result == "InvalidFaderError"
            with pytest.raises(CdspError) as ei:
                await c.command("NoSuchCommand")
            assert ei.value.result == "Invalid"
            with pytest.raises(CdspError) as ei:
                await c.set_config_yaml("garbage: [")
            assert ei.value.result == "ConfigReadError"
            assert c.connect_count == 1  # refusals never reset the link
            await _running(c)  # and the engine kept the good config
            assert await c.get_value("/filters/sub_lowpass/parameters/freq") == 80
            await c.reset_clipped_samples()

    run(body())


@pytest.mark.engine
@pytest.mark.skipif(not os.access(CDSP_BIN, os.X_OK), reason="no camilladsp binary")
def test_engine_audio_follows_commands(engine: Engine) -> None:
    async def body() -> None:
        async with CamillaClient(engine.url) as c:
            await _running(c)
            await c.set_fader(MAIN, 0.0)
            await c.set_fader_mute(MAIN, False)
            await asyncio.sleep(0.7)  # 300 ms ramp
            lv = await c.get_signal_levels()
            assert -24.0 < lv["playback_rms"][0] < -22.0  # -20 dB peak sine = -23 dB rms
            await c.set_fader(AUX1, -10.0)  # subs only
            await asyncio.sleep(0.7)
            lv2 = await c.get_signal_levels()
            assert abs((lv["playback_rms"][0] - lv2["playback_rms"][0]) - 10.0) < 1.0
            assert abs(lv["playback_rms"][2] - lv2["playback_rms"][2]) < 0.5  # tops untouched

            top = "/filters/top_trim/parameters/gain"
            await c.reset_clipped_samples()
            assert await c.set_value(top, 30.0) == 12.0  # client gain ceiling (default +12 dB)
            assert await c.get_value(top) == 12.0
            await asyncio.sleep(0.3)
            assert await c.get_clipped_samples() == 0  # -20 dB peak + 12 dB = -8 dBFS
            # ceiling raised on purpose: the engine path itself does clip at +10 dBFS
            async with CamillaClient(engine.url, gain_max_db=30.0) as loud:
                assert await loud.set_value(top, 30.0) == 30.0
                await asyncio.sleep(0.3)
                assert await c.get_clipped_samples() > 0
                await loud.set_value(top, 0.0)

    run(body())


@pytest.mark.engine
@pytest.mark.skipif(not os.access(CDSP_BIN, os.X_OK), reason="no camilladsp binary")
def test_engine_adjust_fader_never_lifts_below_floor(engine: Engine) -> None:
    """The reviewer's repro: Aux1 at -150 dB, +0.5 dB with min=-6 used to land at -6 dB."""

    async def body() -> None:
        async with CamillaClient(engine.url) as c:
            await _running(c)
            await c.set_fader(MAIN, 0.0)
            await c.set_fader_mute(MAIN, False)
            await c.set_fader(AUX1, -150.0)
            await asyncio.sleep(0.7)  # 300 ms ramp
            before = (await c.get_signal_levels())["playback_rms"][0]
            assert await c.adjust_fader(AUX1, +0.5, min=-6, max=6) == -149.5
            await asyncio.sleep(0.7)
            after = (await c.get_signal_levels())["playback_rms"][0]
            print(f"\n[cdsp] Aux1 -150 dB +0.5 detent (min=-6): sub rms {before:.1f} -> "
                  f"{after:.1f} dB")
            assert after < -100.0 and after - before < 1.0  # still silence: no +144 dB step
            await c.set_fader(MAIN, -60.0)
            assert await c.adjust_fader(MAIN, -0.5, min=-40, max=0) == -60.5
            await c.set_fader(MAIN, -30.0)
            assert await c.adjust_fader(MAIN, -20, min=-40, max=0) == -40.0
            await c.set_fader(AUX2, 10.0)
            assert await c.adjust_fader(AUX2, +0.5, min=-6, max=6) == 6.0
            faders = await c.get_faders()
            assert [faders[i]["volume"] for i in (MAIN, AUX1, AUX2)] == [-40.0, -149.5, 6.0]

    run(body())


def _bad_device_yaml() -> str:
    """Same config, but a playback 'device' that cannot be opened."""
    return engine_yaml().replace("filename: /dev/null", "filename: /nonexistent-dir/out.raw")


@pytest.mark.engine
@pytest.mark.skipif(not os.access(CDSP_BIN, os.X_OK), reason="no camilladsp binary")
def test_engine_set_config_yaml_checks_new_devices(engine: Engine) -> None:
    """File-started engine (the production unit): a device change returns only once it runs;
    a device that cannot open makes the engine exit, which must surface as CdspUnavailable."""

    async def body() -> dict[str, float]:
        out: dict[str, float] = {}
        async with CamillaClient(engine.url, settle_s=0.3) as c:
            await _running(c)
            t = time.perf_counter()
            await c.set_config_yaml(engine_yaml().replace("chunksize: 256", "chunksize: 512"))
            out["device change"] = (time.perf_counter() - t) * 1e3
            assert out["device change"] >= 300  # held for settle_s of steady Running
            assert await c.get_state() == "Running"
            assert (await c.get_config_json())["devices"]["chunksize"] == 512
            # an integer field keeps its type through set_value (usize rejects 512.0)
            got = await c.set_value("/devices/target_level", 512)
            assert got == 512 and type(got) is int
            assert await c.get_value("/devices/target_level") == 512
            await _running(c)

            t = time.perf_counter()
            with pytest.raises(CdspUnavailable):
                await c.set_config_yaml(_bad_device_yaml())
            out["failed device"] = (time.perf_counter() - t) * 1e3
            await asyncio.to_thread(engine.proc.wait, 3)  # type: ignore[union-attr]
        return out

    t = run(body())
    print(f"\n[cdsp] set_config_yaml: device change confirmed running in "
          f"{t['device change']:.0f} ms; failed device -> CdspUnavailable in "
          f"{t['failed device']:.0f} ms (engine exited)")


@pytest.mark.engine
@pytest.mark.skipif(not os.access(CDSP_BIN, os.X_OK), reason="no camilladsp binary")
def test_engine_wait_mode_no_config_and_failed_device(tmp_path: Path) -> None:
    """camilladsp -w: no config (GetConfigJson -> null), then a good upload, then a device
    that cannot open (engine stays up, Inactive, with a stop reason)."""
    eng = Engine(tmp_path, ENGINE_PORT, wait_mode=True)
    eng.start()

    async def body() -> None:
        async with CamillaClient(eng.url, settle_s=0.3) as c:
            assert await c.get_state() == "Inactive"
            with pytest.raises(CdspNoConfig) as ei:
                await c.get_config_json()
            assert ei.value.result == "NoActiveConfig"
            with pytest.raises(CdspError) as ei:
                await c.set_value("/filters/sub_trim/parameters/gain", -1.0)
            assert ei.value.result == "InvalidRequestError"

            await c.set_config_yaml(engine_yaml())  # from nothing: waits for Running
            assert await c.get_state() == "Running"
            assert (await c.get_config_json())["title"] == "k1000 cdsp engine test"

            t = time.perf_counter()
            with pytest.raises(CdspNotRunning) as ei:
                await c.set_config_yaml(_bad_device_yaml())
            elapsed = time.perf_counter() - t
            print(f"\n[cdsp] wait mode, failed device -> CdspNotRunning in {elapsed * 1e3:.0f} ms:"
                  f" {ei.value.message}")
            assert "Inactive" in ei.value.message and "PlaybackError" in ei.value.message
            assert c.connected and c.connect_count == 1
            assert await c.get_state() == "Inactive"

            await c.set_config_yaml(engine_yaml())  # recovery: a good config runs again
            assert await c.get_state() == "Running"

    try:
        run(body())
    finally:
        eng.kill()


@pytest.mark.engine
@pytest.mark.skipif(not os.access(CDSP_BIN, os.X_OK), reason="no camilladsp binary")
def test_engine_kill_restart_reconnects_and_reapplies(engine: Engine) -> None:
    async def body() -> dict[str, float]:
        ups: list[int] = []
        downs: list[int] = []

        async def on_connect(cl: CamillaClient) -> None:
            ups.append(cl.connect_count)
            await cl.set_fader(AUX1, -7.5)  # what the controller does: re-apply its state

        async def on_disconnect(cl: CamillaClient) -> None:
            downs.append(cl.connect_count)

        async def aux1_is(db: float) -> bool:
            return (await c.get_faders())[AUX1]["volume"] == db

        c = CamillaClient(engine.url, on_connect=on_connect, on_disconnect=on_disconnect,
                          backoff_min=0.05, backoff_max=0.5)
        try:
            assert await c.connect(timeout=3.0)
            await until(lambda: aux1_is(-7.5), what="first on_connect re-apply")

            t0 = time.perf_counter()
            await asyncio.to_thread(engine.kill)  # SIGKILL mid-session
            await until(lambda: not c.connected, timeout=2.0, what="disconnect detected")
            detect_s = time.perf_counter() - t0
            with pytest.raises(CdspUnavailable):
                await c.get_state()
            await until(lambda: downs == [1], what="on_disconnect")

            t1 = time.perf_counter()
            await asyncio.to_thread(engine.start)
            await until(lambda: ups == [1, 2], timeout=5.0, what="reconnect + on_connect")
            reconnect_s = time.perf_counter() - t1
            await until(lambda: aux1_is(-7.5), what="re-apply on the fresh engine")
            faders = await c.get_faders()
            assert faders[MAIN] == {"volume": -40.0, "mute": True}  # restarted engine: muted
            await _running(c)
            return {"detect_ms": detect_s * 1e3, "reconnect_ms": reconnect_s * 1e3}
        finally:
            await c.close()

    t = run(body())
    print(f"\n[cdsp] kill detected in {t['detect_ms']:.1f} ms; "
          f"engine start -> on_connect in {t['reconnect_ms']:.1f} ms")


@pytest.mark.engine
@pytest.mark.skipif(not os.access(CDSP_BIN, os.X_OK), reason="no camilladsp binary")
def test_engine_frozen_is_detected_then_recovers(engine: Engine) -> None:
    async def body() -> None:
        busy = CamillaClient(engine.url, timeout=0.5, backoff_min=0.05, backoff_max=0.5)
        idle = CamillaClient(engine.url, ping_interval=0.2, backoff_min=0.05, backoff_max=0.5)
        try:
            assert await busy.connect(3.0) and await idle.connect(3.0)
            await asyncio.sleep(1.0)  # several pings: the engine answers them
            assert idle.connected and idle.connect_count == 1

            engine.freeze()  # a hung engine: TCP alive, nobody answering
            try:
                t0 = time.perf_counter()
                with pytest.raises(CdspTimeout):
                    await busy.get_state()
                assert 0.4 < time.perf_counter() - t0 < 1.5
                await until(lambda: not idle.connected, timeout=2.0, what="ping timeout")
            finally:
                engine.signal(signal.SIGCONT)
            await until(lambda: busy.connect_count >= 2 and idle.connect_count >= 2,
                        timeout=8.0, what="both clients back")
            assert (await busy.get_version()).startswith("5.")
            assert (await idle.get_version()).startswith("5.")
        finally:
            await busy.close()
            await idle.close()

    run(body())


def _stats(samples: list[float]) -> dict[str, float]:
    samples = sorted(samples)
    return {"median": statistics.median(samples),
            "p95": samples[int(len(samples) * 0.95)], "max": samples[-1]}


@pytest.mark.engine
@pytest.mark.skipif(not os.access(CDSP_BIN, os.X_OK), reason="no camilladsp binary")
def test_engine_config_writes_are_never_lost(engine: Engine) -> None:
    """5.0.0-beta.1 loses back-to-back raw config writes; the client's confirmed writes must
    not. The raw control is printed, not asserted (the engine bug is timing-dependent)."""
    a, b, f = ("/filters/sub_trim/parameters/gain", "/filters/sub_delay/parameters/delay",
               "/filters/sub_lowpass/parameters/freq")

    async def body() -> tuple[int, int, int]:
        async with CamillaClient(engine.url) as c:
            await _running(c)
            raw_lost = 0
            for i in range(80):
                want = (-(i % 17) - 0.5, (i % 11) * 0.25 + 0.1, 1000.0 + 100 * i)
                for ptr, v in zip((a, b, f), want):
                    await c.command("SetConfigValue", pointer=ptr, value=v)
                await asyncio.sleep(0.08)
                got = tuple([await c.get_value(p) for p in (a, b, f)])
                raw_lost += got != want
            lost = 0
            for i in range(300):
                want = (-(i % 19) - 0.25, (i % 13) * 0.5 + 0.2, 2000.0 + 50 * i)
                for ptr, v in zip((a, b, f), want):
                    await c.set_value(ptr, v)
                await c.patch_config({"filters": {"live_peq_1": {"parameters": {
                    "gain": -(i % 7) - 1.0, "freq": 300.0 + i}}}})
                got = tuple([await c.get_value(p) for p in (a, b, f)])  # no settle wait needed
                peq = await c.get_value("/filters/live_peq_1/parameters")
                lost += got != want or (peq["gain"], peq["freq"]) != (-(i % 7) - 1.0, 300.0 + i)
            return raw_lost, lost, c.connect_count

    raw_lost, lost, connects = run(body(), limit=60)
    print(f"\n[cdsp] lost writes: raw SetConfigValue bursts {raw_lost}/80, "
          f"client confirmed writes {lost}/300")
    assert lost == 0 and connects == 1


@pytest.mark.engine
@pytest.mark.skipif(not os.access(CDSP_BIN, os.X_OK), reason="no camilladsp binary")
def test_engine_setconfigvalue_latency(engine: Engine) -> None:
    async def body() -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        async with CamillaClient(engine.url) as c:
            await _running(c)
            peq, trim = "/filters/live_peq_1/parameters/gain", "/filters/sub_trim/parameters/gain"
            ack = []  # bare SetConfigValue round trip (spaced so writes cannot collide)
            for i in range(100):
                t = time.perf_counter()
                await c.command("SetConfigValue", pointer=peq, value=-6.0 + (i % 13) * 0.5)
                ack.append((time.perf_counter() - t) * 1e3)
                await asyncio.sleep(0.07)
            out["SetConfigValue ack only, Biquad n=100"] = _stats(ack)
            for label, pointer in (("set_value confirmed, Biquad n=300", peq),
                                   ("set_value confirmed, Gain n=300", trim)):
                samples = []
                for i in range(300):  # encoder-burst pace: back to back
                    t = time.perf_counter()
                    await c.set_value(pointer, -6.0 + (i % 13) * 0.5)
                    samples.append((time.perf_counter() - t) * 1e3)
                out[label] = _stats(samples)
            await c.set_fader(MAIN, -40.0)
            for label, lo in (("adjust_fader, engine floor (1 RTT) n=300", None),
                              ("adjust_fader, min=-80 (read + adjust) n=300", -80.0)):
                samples = []
                for i in range(300):
                    t = time.perf_counter()
                    await c.adjust_fader(MAIN, 0.5 if i % 2 else -0.5, min=lo, max=0.0)
                    samples.append((time.perf_counter() - t) * 1e3)
                out[label] = _stats(samples)
            poll = []  # the controller's ~1 Hz adoption poll
            for _ in range(50):
                t = time.perf_counter()
                await c.get_config_json()
                await c.get_faders()
                poll.append((time.perf_counter() - t) * 1e3)
            out["poll GetConfigJson+GetFaders n=50"] = _stats(poll)
            assert c.connect_count == 1  # no rate-limit errors, no resets
        return out

    stats = run(body(), limit=60)
    for label, s in stats.items():
        print(f"\n[cdsp] RTT {label}: median {s['median']:.2f} ms, "
              f"p95 {s['p95']:.2f} ms, max {s['max']:.2f} ms")
        assert s["median"] < 20.0
