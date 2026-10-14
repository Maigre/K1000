# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for phone protocol 2 (docs/14-phone-v2-protocol.md, DESIGN §10): /v2/ws and /v2/
beside an UNCHANGED /ws, against the REAL Controller over test_web's in-memory engine.

What is pinned here: v1's hello / state / meters / allowlist byte for byte (as on main 04ddb34),
every v2 message, the ALIGN lock (per connection, its expiry, never shared with the knobs),
choose -> set_choice (read-only, stale `seen`, rebuild), grab -> notch_helper(target,
select=False), the spectrum frames (subscribe, rate, drop under backpressure, never ahead of a
reply), the shared mute generation and master bucket, several phones.

No pytest-asyncio on the box (apt only): each test drives its own asyncio.run().
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import math
import os
import re
import shutil
import socket
import ssl
import struct
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from k1000.control import NOTCH_Q, Status
from k1000.profiles import ProfileChoice
from k1000.state import State
from k1000.ui import input_map
from k1000.ui.eqgraph import RtaFrame
from k1000.ui.input_map import SETUP_GROUPS, Mute, setup_schema
from k1000.web import server as web
from k1000.web.server import (LOCKED_V2, MESSAGE_TYPES, NUDGE_CAPS, NUDGE_CAPS_V2, PHONE_RANGES,
                              PROTOCOL_VERSION, SET_BOOL, SET_NUMERIC, PhoneBridge, PhoneServer,
                              PhoneSession, fields_v2, load_static, load_static_all, rta_message)
from test_web import (_self_signed, boot, free_port, http_get, make_ctl, recv_until, run, settings,
                      until)


class FakeRta:
    """Controller.rta stand-in: with none, snapshot() clears the spectrum (no task, no picture)."""

    def note_input(self) -> None:
        pass


class Clock:
    """A clock the test moves (the bridge's buckets and the ALIGN unlock read it)."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def origin(srv: PhoneServer) -> str:
    return f"http://127.0.0.1:{srv.ports['plain']}"


def v2url(srv: PhoneServer) -> str:
    return f"ws://127.0.0.1:{srv.ports['plain']}/v2/ws"


def v1url(srv: PhoneServer) -> str:
    return f"ws://127.0.0.1:{srv.ports['plain']}/ws"


async def send(ws: Any, **msg: Any) -> None:
    await ws.send(json.dumps(msg))


async def reply(ws: Any, mid: Any, timeout: float = 2.0) -> dict[str, Any]:
    return await recv_until(ws, lambda m: m.get("t") in ("ack", "err") and m.get("id") == mid, timeout)


def sess(n: int = 1) -> PhoneSession:
    return PhoneSession(n=n)


# ============================================================================ v1 stays v1
# Produced by main 04ddb34's server.py (git archive) for the inputs of _v1_inputs(): the v1 wire,
# byte for byte. A change here is a change of protocol 1: the v1 page would break.
V1_HELLO = '{"t": "hello", "v": 1, "build": "abc", "phone_on": true, "ranges": {"master_db": {"min": -80.0, "max": 0.0, "step": 0.5, "log": false, "fine": null, "unit": "dB"}, "balance_db": {"min": -12.0, "max": 12.0, "step": 0.5, "log": false, "fine": null, "unit": "dB"}, "lowcut_hz": {"min": 20.0, "max": 300.0, "step": 2.0, "log": false, "fine": 1.0, "unit": "Hz"}, "live_peq.freq": {"min": 20.0, "max": 20000.0, "step": 0.08333333333333333, "log": true, "fine": 0.020833333333333332, "unit": "Hz"}, "live_peq.gain": {"min": -20.0, "max": 6.0, "step": 0.5, "log": false, "fine": null, "unit": "dB"}, "live_peq.q": {"min": 0.3, "max": 16.0, "step": 0.16666666666666666, "log": true, "fine": 0.041666666666666664, "unit": ""}, "live_lowshelf.freq": {"min": 30.0, "max": 1000.0, "step": 0.08333333333333333, "log": true, "fine": 0.020833333333333332, "unit": "Hz"}, "live_lowshelf.gain": {"min": -12.0, "max": 6.0, "step": 0.5, "log": false, "fine": null, "unit": "dB"}, "live_lowshelf.q": {"min": 0.4, "max": 1.5, "step": 0.16666666666666666, "log": true, "fine": 0.041666666666666664, "unit": ""}, "live_highshelf.freq": {"min": 1000.0, "max": 16000.0, "step": 0.08333333333333333, "log": true, "fine": 0.020833333333333332, "unit": "Hz"}, "live_highshelf.gain": {"min": -12.0, "max": 6.0, "step": 0.5, "log": false, "fine": null, "unit": "dB"}, "live_highshelf.q": {"min": 0.4, "max": 1.5, "step": 0.16666666666666666, "log": true, "fine": 0.041666666666666664, "unit": ""}}, "bands": ["live_peq.0", "live_peq.1", "live_peq.2", "live_peq.3", "live_lowshelf", "live_highshelf"], "labels": ["PEQ 1", "PEQ 2", "PEQ 3", "PEQ 4", "LO SHELF", "HI SHELF"], "short": ["P1", "P2", "P3", "P4", "LS", "HS"], "notch": {"gain": -6.0, "q": 8.0}, "nudge": {"master_db": [-12, 2], "balance_db": [-4, 4], "lowcut_hz": [-12, 12], "live_peq.0.freq": [-24, 24], "live_peq.0.gain": [-12, 12], "live_peq.0.q": [-12, 12], "live_peq.1.freq": [-24, 24], "live_peq.1.gain": [-12, 12], "live_peq.1.q": [-12, 12], "live_peq.2.freq": [-24, 24], "live_peq.2.gain": [-12, 12], "live_peq.2.q": [-12, 12], "live_peq.3.freq": [-24, 24], "live_peq.3.gain": [-12, 12], "live_peq.3.q": [-12, 12], "live_lowshelf.freq": [-24, 24], "live_lowshelf.gain": [-12, 12], "live_lowshelf.q": [-12, 12], "live_highshelf.freq": [-24, 24], "live_highshelf.gain": [-12, 12], "live_highshelf.q": [-12, 12]}}'  # noqa: E501
V1_STATE = '{"t": "state", "master_db": -20.0, "muted": false, "hold": "mute", "mute_seq": 5, "mute_known": true, "balance_db": 3.0, "lowcut_on": true, "lowcut_hz": 112.0, "bands": [{"f": 250.0, "g": 0.0, "q": 4.0, "on": true}, {"f": 1000.0, "g": -6.5, "q": 4.0, "on": true}, {"f": 2500.0, "g": 0.0, "q": 4.0, "on": false}, {"f": 5000.0, "g": 0.0, "q": 4.0, "on": true}, {"f": 120.0, "g": 0.0, "q": 0.707, "on": true}, {"f": 8000.0, "g": 0.0, "q": 0.707, "on": true}], "engine": "Running", "linked": true, "device_present": true, "device": "MOTU M4", "profile": "m4", "message": "m", "drift": ["x"], "ramping": false, "load": 4.2, "phone_on": true, "phones": 2}'  # noqa: E501
V1_METERS = '{"t": "m", "l": [-12.5, -60.0, -3.0, 0.0], "c": [false, true, false, false]}'
V1_TYPES = ["ping", "nudge", "set", "band", "notch", "mute", "reset_live"]
V1_CAPS = {"master_db": [-12, 2], "balance_db": [-4, 4], "lowcut_hz": [-12, 12],
           **{f"{b}.{p}": ([-24, 24] if p == "freq" else [-12, 12])
              for b in ("live_peq.0", "live_peq.1", "live_peq.2", "live_peq.3", "live_lowshelf",
                        "live_highshelf") for p in ("freq", "gain", "q")}}


def _v1_inputs() -> tuple[State, Status]:
    st = State()
    st.live_peq[1].gain = -6.5
    st.live_peq[2].on = False
    st.lowcut_hz, st.balance_db = 112.0, 3.0
    status = Status(engine="Running", load=4.24, levels=[-12.26, -70.0, -3.0, 0.5],
                    clip=[False, True, False, False], muted=False, device="MOTU M4", profile="m4",
                    device_present=True, drift=["x"], connected=True, message="m")
    return st, status


def test_v1_hello_state_and_allowlist_are_pinned() -> None:
    """Protocol 1 as on main 04ddb34 (D-014 included), byte for byte: /ws is untouched."""
    st, status = _v1_inputs()

    class Ctl:
        def snapshot(self) -> Any:
            return "SHOW", st, status, None

        def displayed_muted(self) -> bool:
            return False

    hello = PhoneBridge(Ctl()).hello(True, "abc")
    assert hello.pop("server").startswith("k1000 ")
    assert json.dumps(hello) == V1_HELLO
    assert json.dumps(PhoneBridge.state_message(("SHOW", st, status, None), phone_on=True,
                                                phones=2, mute=(False, 5))) == V1_STATE
    assert json.dumps(PhoneBridge.meters_message(status)) == V1_METERS
    assert PROTOCOL_VERSION == 1 and list(MESSAGE_TYPES) == V1_TYPES
    assert {k: list(v) for k, v in NUDGE_CAPS.items()} == V1_CAPS
    assert SET_BOOL == {"lowcut_on"} and SET_NUMERIC == {
        f"{b}.{p}" for b in input_map.LIVE_SLOTS for p in ("freq", "gain", "q")}
    assert PHONE_RANGES[:3] == ("master_db", "balance_db", "lowcut_hz") and len(PHONE_RANGES) == 12


def test_v1_build_ignores_the_v2_files(tmp_path: Path) -> None:
    """A v2 deploy never makes a v1 page reload itself (hello.build), and the reverse."""
    dst = tmp_path / "static"
    shutil.copytree(web.STATIC_DIR, dst)
    shutil.rmtree(dst / "v2", ignore_errors=True)
    _a, v1_alone = load_static(dst)
    (dst / "v2").mkdir()
    (dst / "v2" / "index.html").write_text("<!doctype html><title>x</title>")
    (dst / "v2" / "app.js").write_text("const BUILD = '__K1000_BUILD__';")
    (dst / "v2" / "mod.mjs").write_text("export const B = '__K1000_BUILD__';")
    (dst / "v2" / "sw.js").write_text("const BUILD = '__K1000_BUILD__';")
    assets, builds = load_static_all(dst)
    assert builds["v1"] == v1_alone == load_static(dst)[1]
    b2 = builds["v2"]
    assert b2 != builds["v1"]
    for p in ("/v2/app.js", "/v2/mod.mjs", "/v2/sw.js"):
        assert assets[p].body.decode().count(b2) == 1 and b"__K1000_BUILD__" not in assets[p].body
    assert builds["v1"].encode() in assets["/app.js"].body and b2.encode() not in assets["/app.js"].body
    (dst / "v2" / "app.js").write_text("const BUILD = '__K1000_BUILD__'; // v2 changed")
    _assets, builds2 = load_static_all(dst)
    assert builds2["v1"] == builds["v1"] and builds2["v2"] != b2
    (dst / "app.css").write_text((dst / "app.css").read_text() + "\n/* v1 changed */\n")
    assert load_static_all(dst)[1]["v2"] == builds2["v2"]


def test_v1_socket_is_unchanged_beside_v2(tmp_path: Path) -> None:
    """/ws: hello v1 with exactly v1's keys, state with exactly v1's keys, v2 messages unknown."""
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        srv = PhoneServer(ctl, settings(tmp_path), tick_s=0.02)
        await srv.start()
        async with ws_connect(v1url(srv), origin=origin(srv)) as ws:
            hello = json.loads(await ws.recv())
            assert hello["v"] == 1 and list(hello) == ["t", "v", "server", "build", "phone_on", "ranges",
                                                       "bands", "labels", "short", "notch", "nudge"]
            assert hello["build"] == srv.build != srv.build_v2
            st = await recv_until(ws, lambda m: m["t"] == "state")
            assert list(st) == list(json.loads(V1_STATE))
            m = await recv_until(ws, lambda m: m["t"] == "m")
            assert set(m) == {"t", "l", "c"}
            for t in ("grab", "choose", "unlock", "sub"):
                await send(ws, t=t, id=t)
                e = await reply(ws, t)
                assert e == {"t": "err", "id": t, "why": "unknown message"}     # no `code` on v1
            await send(ws, t="nudge", f="align_delay_ms", d=1, id="d")          # not a v1 control
            assert (await reply(ws, "d"))["why"].startswith("not a phone control")
            await send(ws, t="nudge", f="lowcut_hz", d=1, id="lc")              # v1: no lock (P0-b is CSS)
            assert (await reply(ws, "lc"))["t"] == "ack"
        await srv.stop()
        await ctl.stop()
    run(go())


# ============================================================================ static, origin
def test_v2_static_files_scope_and_origin(tmp_path: Path) -> None:
    async def go() -> None:
        static = tmp_path / "static"
        shutil.copytree(web.STATIC_DIR, static)
        (static / "v2" / "sw.js").write_text("const BUILD = '__K1000_BUILD__';\n")
        (static / "v2" / "app.mjs").write_text("export const BUILD = '__K1000_BUILD__';\n")
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        srv = PhoneServer(ctl, settings(tmp_path), static_dir=static)
        await srv.start()
        port = srv.ports["plain"]
        st, h, body = await http_get(port, "/v2")
        assert st == 302 and h["location"] == "/v2/"
        st, h, body = await http_get(port, "/v2/")
        assert st == 200 and h["content-type"].startswith("text/html") and b"K1000 v2" in body
        assert f"ws://127.0.0.1:{port}" in h["content-security-policy"]
        assert "script-src 'self'" in h["content-security-policy"]
        assert (await http_get(port, "/v2/index.html"))[2] == body
        # a navigation under /v2/ cannot be cached by v1's worker under "/" (Vary: *); the
        # v2 worker's install-time fetches, and every v1 response, carry no Vary
        nav = {"Sec-Fetch-Mode": "navigate"}
        assert (await http_get(port, "/v2/", headers=nav))[1]["vary"] == "*"
        assert "vary" not in (await http_get(port, "/v2/", headers={"Sec-Fetch-Mode": "cors"}))[1]
        assert "vary" not in h
        assert "vary" not in (await http_get(port, "/", headers=nav))[1]
        assert "vary" not in (await http_get(port, "/v2/app.mjs", headers=nav))[1]
        st, h, body = await http_get(port, "/v2/sw.js")
        assert st == 200 and h["service-worker-allowed"] == "/v2/" and srv.build_v2.encode() in body
        st, h, body = await http_get(port, "/v2/app.mjs")
        assert st == 200 and h["content-type"].startswith("text/javascript")
        assert srv.build_v2.encode() in body
        assert (await http_get(port, "/sw.js"))[1]["service-worker-allowed"] == "/"
        for bad in ("/v2/nope.js", "/v2/../server.py", "/v2/%2e%2e/app.js", "/v2/.hidden"):
            assert (await http_get(port, bad))[0] == 404, bad
        with pytest.raises(Exception):
            async with ws_connect(v2url(srv), origin="http://evil.example"):
                pass
        assert srv.stats["refused"] >= 1
        await srv.stop()
        await ctl.stop()
    run(go())


# ============================================================================ hello and state
def test_v2_hello_carries_the_schema(tmp_path: Path) -> None:
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        srv = PhoneServer(ctl, settings(tmp_path, min_app=3), tick_s=0.02)
        await srv.start()
        async with ws_connect(v2url(srv), origin=origin(srv)) as ws:
            h = json.loads(await ws.recv())
            assert h["t"] == "hello" and h["v"] == 2 and h["you"] == 1 and h["min_app"] == 3
            assert h["build"] == srv.build_v2 and h["phone_on"] is True
            assert h["surface"] == {"screen": True, "knobs": True}
            assert h["order"] == [4, 0, 1, 2, 3, 5] and h["bells"] == [0, 1, 2, 3]
            assert h["kind"][4:] == ["low_shelf", "high_shelf"]
            assert h["nudge"] == {k: list(v) for k, v in NUDGE_CAPS_V2.items()}
            assert h["acts"] == ["reset_live", "undo_reset", "laptop_open", "laptop_close"]
            assert h["unlock_s"] == input_map.UNLOCK_S == 30.0
            assert h["notch"]["q"] == pytest.approx(NOTCH_Q, abs=1e-3)
            assert h["limits"]["max_phones"] == 4 and h["limits"]["master_up_db_per_s"] == 6.0
            assert h["limits"]["guard_db"] == 6.0 and h["limits"]["guard_window_s"] == 2.0
            assert h["rta"] == {"enabled": True, "src": "PGM", "hz": 10.0, "step_db": 0.5,
                                "idle_s": web.RTA_SUB_IDLE_S}
            assert not any(k.startswith("trim_up") for k in h["limits"])     # one PA bucket
            assert h["slide"]["px_per_detent"] == 14 and h["slide"]["master_up_px_per_detent"] == 20
            assert [g["group"] for g in h["setup"]] == [name for name, _ in SETUP_GROUPS]
            json.dumps(h, allow_nan=False)
        await srv.stop()
        await ctl.stop()
    run(go())


def test_fields_are_the_box_rules() -> None:
    f = fields_v2()
    assert set(f) == {"master_db", "balance_db", "lowcut_hz", "align_delay_ms", "out_trim_db",
                      "input_trim_db", "sub_lowpass_hz",
                      *(f"{b}.{p}" for b in ("live_peq", "live_lowshelf", "live_highshelf")
                        for p in ("freq", "gain", "q"))}
    paths = [p for e in f.values() for p in e["paths"]]
    assert sorted(paths) == sorted(NUDGE_CAPS_V2)
    for key, e in f.items():
        lo, hi = e["caps"]
        assert all(lo <= d <= hi and d != 0 for d in e["stepper"]), key
        assert (e["lock"] == "align") == (key in LOCKED_V2), key
        rng = web.range_for(e["paths"][0])
        assert (e["min"], e["max"], e["step"], e["log"]) == (rng.min, rng.max, rng.step, rng.log)
    m = f["master_db"]
    assert m["accel"] == {"up": 1, "down": 4} and m["up_px_per_detent"] == 20 and m["caps"] == [-12, 2]
    assert m["up_limit"] == {"db_per_s": 6.0, "burst_db": 3.0, "bucket": "pa"}
    assert m["stepper"] == [-12, -2, -1, 1, 2]
    assert f["live_peq.freq"]["accel"] == {"up": 4, "down": 4} and f["live_peq.freq"]["landmarks"] == [100.0, 1000.0, 10000.0]
    assert f["live_peq.gain"]["accel"] == {"up": 1, "down": 1} and f["live_peq.gain"]["sticky"] == [0.0]
    assert f["live_lowshelf.q"]["label"] == "SLOPE" and f["live_peq.q"]["label"] == "Q"
    d = f["align_delay_ms"]
    assert d["fine"] == pytest.approx(1000 / 48000) and d["step"] == 0.1 and d["stepper"] == [-10, -1, 1, 10]
    assert d["min"] == -50.0 and d["sticky"] == [0.0]
    lc = f["lowcut_hz"]
    assert lc["snap"] is True and lc["step"] == 2.0 and lc["fine"] == 1.0 and lc["accel"] == {"up": 4, "down": 4}
    assert f["out_trim_db"]["paths"] == [f"out_trim_db.{i}" for i in range(4)]
    # the trims rise through master's bucket: the same `up_limit`, the same bucket name
    assert f["out_trim_db"]["up_limit"] == f["input_trim_db"]["up_limit"] == m["up_limit"]
    assert "up_limit" not in f["balance_db"] and "up_limit" not in f["live_peq.gain"]
    assert input_map.knob_accel("live_peq.2.freq") == (4, 4) and input_map.knob_accel("out_trim_db.1") is None


def test_setup_schema_is_setup_groups() -> None:
    sch = setup_schema()
    assert [(g["group"], [(r["label"], r["f"], r["kind"]) for r in g["items"]]) for g in sch] == [
        (name, [(it.label, it.field, it.kind) for it in items]) for name, items in SETUP_GROUPS]
    rows = {r["f"]: r for g in web.setup_v2() for r in g["items"]}
    assert rows["top_inverted"]["names"] == ["normal", "INVERTED"] and rows["top_inverted"]["rebuild"] is False
    assert rows["mono_subs"]["rebuild"] is True and rows["mono_subs"]["names"] == ["off", "ON"]
    assert rows["profile"]["choices"] == ["dev", "m4"] and rows["profile"]["names"] == ["DEV", "M4"]
    assert rows["dev_audition"]["rebuild"] is True
    assert rows["reset_live"]["confirm"] is True and rows["reset_live"]["act"] == ["reset_live"]
    assert rows["laptop_access"]["act"] == ["laptop_open", "laptop_close"]
    assert rows["PHONE"]["phone"] == "readonly" and rows["HWTEST"]["phone"] == "hidden"
    assert rows["out_trim_db.2"]["field"] == "out_trim_db" and rows["out_trim_db.2"]["phone"] == "edit"
    assert all(r["phone"] == "edit" for f, r in rows.items() if r["kind"] in ("bool", "choice"))


def test_state2_is_strict_json_and_complete(tmp_path: Path) -> None:
    async def go() -> None:
        st = State()
        st.sub_inverted, st.mono_subs = True, True
        st.out_trim_db[2] = -1.5
        st.sub_delay_ms = 1.25
        ctl, _ = make_ctl(tmp_path, st, settings={"spectrum": {"enabled": False}},
                          profile_choice=ProfileChoice("dev", "settings"))
        await boot(ctl)
        ctl.show_hint("P2 OUT")
        ctl.set_knobs_ok(False)
        ctl.status.load = float("nan")
        ctl.status.in_peak_db = [float("-inf"), -12.26]
        snap = ctl.snapshot()
        s = PhoneBridge.state_message2(snap, phone_on=True, phones=1, mute=(False, 3), knobs=True)
        back = json.loads(json.dumps(s, allow_nan=False))
        assert set(json.loads(V1_STATE)) < set(back)
        assert back["setup"]["sub_inverted"] is True and back["setup"]["mono_subs"] is True
        assert back["setup"]["out_trim_db.2"] == -1.5 and back["setup"]["profile"] == "dev"
        assert back["setup"]["spectrum_on"] is True and back["align_delay_ms"] == 1.25
        assert back["readonly"]["profile"] == "PROFILE PINNED IN k1000.yaml"
        assert back["readonly"]["spectrum_on"] == "SPECTRUM OFF IN k1000.yaml"
        assert back["spectrum"] == {"on": False, "src": "PGM", "state": "disabled"}
        assert back["hint"] == "P2 OUT" and 1 <= back["hint_s"] <= 3
        assert ["critical", "KNOBS OFFLINE · SOURCE DOWN / BYPASS"] in back["alerts"]
        assert back["knobs_ok"] is False and back["mute_fallback"] == "turn the source down · bypass"
        assert "POL INV SUB" in back["pills"] and "MONO SUB" in back["pills"]
        assert back["box_band"] == 0 and back["box_page"] == "SHOW" and back["load"] is None
        assert back["laptop"] == {"state": "off", "s": 0} and back["undo_reset_s"] == 0
        assert back["mute_band"] == {"kind": "", "text": ""} and back["mute_src"] == ""
        m = PhoneBridge.meters_message2(snap[2])
        assert m["i"] == [-60.0, -12.5] and json.dumps(m, allow_nan=False)
        await ctl.stop()
    run(go())


def test_mute_band_follows_the_box_rule() -> None:
    st = State()
    st.muted = False
    ramp = Status(muted=True, ramping=True)
    assert web._mute_band(st, ramp) == {"kind": "ramp", "text": "RAMPS IN BY ITSELF"}
    st.muted = True
    assert web._mute_band(st, Status(muted=True, alert="APPLYING SETUP…")) == {
        "kind": "alert", "text": "APPLYING SETUP…"}
    assert web._mute_band(st, Status(muted=True, engine="Running", ready=True))["kind"] == "ready"
    assert web._mute_band(st, Status(muted=True, engine="Paused", ready=True))["kind"] == "wait"
    assert web._mute_band(st, Status(muted=False))["kind"] == ""


# ============================================================================ nudge and the lock
def test_v2_nudge_allowlist_and_values(tmp_path: Path) -> None:
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        b, s = PhoneBridge(ctl), sess()
        assert b.apply2({"t": "nudge", "f": "out_trim_db.1", "d": -2}, s) == (True, -1.0, "", "")
        assert b.apply2({"t": "nudge", "f": "input_trim_db", "d": -1, "fine": True}, s)[:2] == (True, -0.5)
        assert b.apply2({"t": "nudge", "f": "sub_lowpass_hz", "d": 12}, s)[1] == pytest.approx(240.0, abs=0.01)
        assert b.apply2({"t": "nudge", "f": "live_peq.2.gain", "d": -6}, s)[1] == -3.0
        for bad, code in (({"f": "sys_peq.0.gain", "d": 1}, "BAD"), ({"f": "out_trim_db.4", "d": 1}, "BAD"),
                          ({"f": "master_db", "d": 3}, "BAD"), ({"f": "balance_db", "d": 0}, "BAD"),
                          ({"f": "balance_db", "d": 1.0}, "BAD"), ({"f": "balance_db", "d": True}, "BAD"),
                          ({"f": "mono_subs", "d": 1}, "BAD"), ({"f": ["x"], "d": 1}, "BAD")):
            ok, _v, _why, c = b.apply2({"t": "nudge", **bad}, s)
            assert not ok and c == code, bad
        assert ctl.state.out_trim_db[1] == -1.0 and ctl.state.input_trim_db == -0.5
        for t in ("set", "notch", "wat"):
            assert b.apply2({"t": t}, s)[3] == "UNKNOWN"
        await ctl.stop()
    run(go())


def test_align_lock_is_enforced_per_connection_and_expires(tmp_path: Path) -> None:
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        clk = Clock()
        b = PhoneBridge(ctl, clock=clk)
        a, other = sess(1), sess(2)
        lc0 = ctl.state.lowcut_hz
        for f in ("lowcut_hz", "align_delay_ms"):
            ok, v, why, code = b.apply2({"t": "nudge", "f": f, "d": 1}, a)
            assert (ok, code, why) == (False, "LOCKED", "LOCKED · HOLD 0.8 s TO EDIT")
            assert v == ctl.state.get(f)
        assert ctl.state.lowcut_hz == lc0 and ctl.state.sub_delay_ms == 0.0
        # the KNOBS' unlock (B2 hold on ALIGN) never unlocks a phone
        ctl.focus.unlock_until = time.monotonic() + 30.0
        assert b.apply2({"t": "nudge", "f": "lowcut_hz", "d": 1}, a)[3] == "LOCKED"
        ctl.focus.unlock_until = 0.0
        assert b.apply2({"t": "unlock", "what": "nope"}, a)[3] == "BAD"
        assert b.apply2({"t": "unlock", "what": "align"}, a) == (True, {"align_s": 30.0}, "", "")
        # ...and a phone's unlock never unlocks the knobs
        assert ctl.focus.unlocked() is False and ctl.snapshot()[2].align_locked is True
        assert b.apply2({"t": "nudge", "f": "lowcut_hz", "d": 1}, a)[:2] == (True, lc0 + 2.0)
        assert b.apply2({"t": "nudge", "f": "align_delay_ms", "d": 3}, a)[:2] == (True, 0.3)
        assert ctl.state.sub_delay_ms == pytest.approx(0.3)
        # another phone stays locked
        assert b.apply2({"t": "nudge", "f": "lowcut_hz", "d": 1}, other)[3] == "LOCKED"
        # every accepted move renews the window; 30 s idle relocks
        clk.t += 29.0
        assert b.apply2({"t": "nudge", "f": "align_delay_ms", "d": -3}, a)[:2] == (True, 0.0)
        clk.t += 29.0
        assert b.apply2({"t": "nudge", "f": "align_delay_ms", "d": -1}, a)[:2] == (True, -0.1)
        assert ctl.state.top_delay_ms == pytest.approx(0.1)          # D-011: tops later
        assert b.unlock_left(a) == 30.0
        clk.t += 30.0
        assert b.apply2({"t": "nudge", "f": "align_delay_ms", "d": 1}, a)[3] == "LOCKED"
        assert a.unlock_until == 0.0
        # lock ends it at once
        b.apply2({"t": "unlock", "what": "align"}, a)
        assert b.apply2({"t": "lock"}, a) == (True, {"align_s": 0.0}, "", "")
        assert b.apply2({"t": "nudge", "f": "lowcut_hz", "d": -1}, a)[3] == "LOCKED"
        # BAL is never locked
        assert b.apply2({"t": "nudge", "f": "balance_db", "d": 1}, other)[0] is True
        await ctl.stop()
    run(go())


def test_lock_over_the_socket_and_the_idle_notice(tmp_path: Path) -> None:
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        clk = Clock(time.monotonic())
        srv = PhoneServer(ctl, settings(tmp_path), tick_s=0.02, clock=clk)
        await srv.start()
        async with ws_connect(v2url(srv), origin=origin(srv)) as ws, \
                ws_connect(v2url(srv), origin=origin(srv)) as ws2:
            assert json.loads(await ws.recv())["you"] == 1
            assert json.loads(await ws2.recv())["you"] == 2
            await send(ws, t="nudge", f="lowcut_hz", d=2, id=1)
            e = await reply(ws, 1)
            assert e["code"] == "LOCKED" and e["v"] == 100.0 and e["why"].startswith("LOCKED")
            await send(ws, t="unlock", what="align", id=2)
            assert (await reply(ws, 2))["v"] == {"align_s": 30.0}
            await send(ws, t="nudge", f="lowcut_hz", d=2, id=3)
            ack = await reply(ws, 3)
            assert ack == {"t": "ack", "id": 3, "v": 104.0, "unlock_s": 30.0}
            await send(ws2, t="nudge", f="lowcut_hz", d=2, id=4)
            assert (await reply(ws2, 4))["code"] == "LOCKED"
            await send(ws, t="nudge", f="lowcut_hz", d=-1)                 # no id: no ack
            await until(lambda: ctl.state.lowcut_hz == 102.0, what="id-less nudge applied")
            clk.t += 31.0
            note = await recv_until(ws, lambda m: m["t"] == "locked")
            assert note == {"t": "locked", "what": "align", "why": "idle"}
            await send(ws, t="nudge", f="lowcut_hz", d=1, id=5)
            assert (await reply(ws, 5))["code"] == "LOCKED"
            assert ctl.state.lowcut_hz == 102.0
        await srv.stop()
        await ctl.stop()
    run(go())


def test_master_up_over_v2_never_beats_the_guards(tmp_path: Path) -> None:
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        clk = Clock()
        b = PhoneBridge(ctl, clock=clk)
        s = sess()
        # the shared bucket: 3 dB of burst, then SLOW (dropped, never queued)
        oks = [b.apply2({"t": "nudge", "f": "master_db", "d": 2}, s) for _ in range(4)]
        assert [o[0] for o in oks] == [True, True, True, False]
        assert oks[3][2:] == ("MASTER: SLOW DOWN", "SLOW") and oks[3][1] == -17.0
        assert ctl.state.master_db == -17.0
        assert b.apply2({"t": "nudge", "f": "master_db", "d": -12}, s)[0] is True    # down: never limited
        # v1 and v2 share the bucket
        clk.t += 0.5                                     # +3 dB of tokens
        assert b.apply({"t": "nudge", "f": "master_db", "d": 2})[0] is True
        assert b.apply({"t": "nudge", "f": "master_db", "d": 2})[0] is True
        assert b.apply2({"t": "nudge", "f": "master_db", "d": 2}, s)[0] is True
        assert b.apply2({"t": "nudge", "f": "master_db", "d": 2}, s)[3] == "SLOW"
        assert b.apply({"t": "nudge", "f": "master_db", "d": 2})[2] == "master: rising too fast"
        # a generous bucket still meets the Controller's own guard (+6 dB per 2 s)
        b2 = PhoneBridge(ctl, clock=clk, master_up_db_per_s=40.0)
        ctl._master_ups.clear()
        got = [b2.apply2({"t": "nudge", "f": "master_db", "d": 2}, s) for _ in range(8)]
        assert sum(1 for o in got if o[0]) == 6 and got[-1][3] == "SLOW"
        # the trims meet the same bucket, empty now (test_trims_and_master_rise_through_one_bucket)
        tr = [b.apply2({"t": "nudge", "f": "input_trim_db", "d": 4}, s) for _ in range(3)]
        assert [o[0] for o in tr] == [False, False, False] and tr[0][2:] == ("TRIM: SLOW DOWN", "SLOW")
        assert b.apply2({"t": "nudge", "f": "input_trim_db", "d": -4}, s)[0] is True
        await ctl.stop()
    run(go())


def test_trims_and_master_rise_through_one_bucket(tmp_path: Path) -> None:
    """Review S1 #4: raising a trim raises the PA like master, so master and the trims (input
    and outputs, every phone, v1 and v2) share ONE bucket: 3 dB of burst, 6 dB/s, in all."""
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        clk = Clock()
        b = PhoneBridge(ctl, clock=clk)
        p1, p2 = sess(1), sess(2)
        m0, t0 = ctl.state.master_db, ctl.state.input_trim_db
        # the same instant: master +3 dB takes the whole burst, a trim rise is then SLOW
        assert b.apply2({"t": "nudge", "f": "master_db", "d": 2}, p1)[0] is True
        assert b.apply2({"t": "nudge", "f": "master_db", "d": 2}, p1)[0] is True
        assert b.apply2({"t": "nudge", "f": "master_db", "d": 2}, p1)[0] is True
        ok, v, why, code = b.apply2({"t": "nudge", "f": "input_trim_db", "d": 4}, p2)
        assert (ok, v, why, code) == (False, t0, "TRIM: SLOW DOWN", "SLOW")
        assert b.apply2({"t": "nudge", "f": "out_trim_db.2", "d": 1}, p2)[3] == "SLOW"
        assert ctl.state.master_db == m0 + 3.0 and ctl.state.input_trim_db == t0
        # ...and the reverse: trims spend it, then master (v1 too) is refused
        clk.t += 0.5                                     # +3 dB of tokens
        assert b.apply2({"t": "nudge", "f": "input_trim_db", "d": 4}, p2)[0] is True
        assert b.apply2({"t": "nudge", "f": "out_trim_db.0", "d": 2}, p1)[0] is True
        assert b.apply2({"t": "nudge", "f": "master_db", "d": 1}, p1)[3] == "SLOW"
        assert b.apply({"t": "nudge", "f": "master_db", "d": 1})[2] == "master: rising too fast"
        # sustained, everything together stays at the bucket's rate: 6 dB in 1 s, not 6 + 6
        clk.t += 0.5
        rose = 0.0
        for k in range(20):
            clk.t += 0.05
            f = ("master_db", "input_trim_db", "out_trim_db.1")[k % 3]
            if b.apply2({"t": "nudge", "f": f, "d": 1}, p1)[0]:
                rose += 0.5
        assert rose <= 3.0 + 6.0 * 1.0 + 1e-9
        # down never waits
        assert b.apply2({"t": "nudge", "f": "input_trim_db", "d": -4}, p2)[0] is True
        assert b.apply2({"t": "nudge", "f": "master_db", "d": -12}, p1)[0] is True
        await ctl.stop()
    run(go())


# ============================================================================ choose
def test_choose_goes_through_set_choice(tmp_path: Path) -> None:
    async def go() -> None:
        ctl, eng = make_ctl(tmp_path, settings={"spectrum": {"enabled": False}},
                            profile_choice=ProfileChoice("dev", "settings"))
        await boot(ctl)
        b, s = PhoneBridge(ctl), sess()
        calls: list[tuple[str, Any]] = []
        real = ctl.set_choice

        def spy(f: str, v: Any) -> bool:
            calls.append((f, v))
            return real(f, v)
        ctl.set_choice = spy                        # type: ignore[method-assign]
        assert b.apply2({"t": "choose", "f": "sub_inverted", "v": True, "seen": False, "id": 1}, s) == (
            True, True, "", "")
        assert ctl.state.sub_inverted is True and calls == [("sub_inverted", True)]
        # stale: the value moved since the phone looked; nothing is written
        ok, v, why, code = b.apply2({"t": "choose", "f": "sub_inverted", "v": False, "seen": False}, s)
        assert (ok, v, why, code) == (False, True, "CHANGED MEANWHILE", "STALE")
        assert len(calls) == 1 and ctl.state.sub_inverted is True
        assert b.apply2({"t": "choose", "f": "sub_inverted", "v": True, "seen": True}, s)[3] == "NOCHANGE"
        # read-only rows: the box's reason
        ok, v, why, code = b.apply2({"t": "choose", "f": "profile", "v": "m4", "seen": "dev"}, s)
        assert (ok, v, code) == (False, "dev", "READONLY") and why == "PROFILE PINNED IN k1000.yaml"
        ok, _v, why, code = b.apply2({"t": "choose", "f": "spectrum_on", "v": False, "seen": True}, s)
        assert code == "READONLY" and why == "SPECTRUM OFF IN k1000.yaml"
        # types are strict: a string is not a bool, a choice must be one of them
        for bad in ({"f": "sub_inverted", "v": "true", "seen": False}, {"f": "sub_inverted", "v": 1, "seen": False},
                    {"f": "sub_inverted", "v": True}, {"f": "dev_audition", "v": "loud", "seen": "sum"},
                    {"f": "master_db", "v": -10.0, "seen": -20.0}, {"f": "muted", "v": True, "seen": False},
                    {"f": "out_trim_db.0", "v": 1.0, "seen": 0.0}):
            assert b.apply2({"t": "choose", **bad}, s)[3] == "BAD", bad
        # Lo-Cut: the box's own path (set_lowcut)
        assert b.apply2({"t": "choose", "f": "lowcut_on", "v": False, "seen": True}, s)[:2] == (True, False)
        assert ctl.state.lowcut_on is False and ctl.pills()[0] == "LO-CUT OFF"
        # a rebuild row: set_choice confirms it; the worker rebuilds the pipeline
        n_up = 0
        orig = eng.set_config_yaml

        async def counting(text: str, timeout: float = 10.0) -> None:
            nonlocal n_up
            n_up += 1
            await orig(text, timeout)
        eng.set_config_yaml = counting               # type: ignore[method-assign]
        assert b.apply2({"t": "choose", "f": "mono_subs", "v": True, "seen": False}, s)[:2] == (True, True)
        assert await ctl.settle(timeout=3.0)
        assert ctl.state.mono_subs is True and n_up >= 1
        assert b.apply2({"t": "choose", "f": "dev_audition", "v": "tops", "seen": "sum"}, s)[:2] == (True, "tops")
        await ctl.stop()
    run(go())


# ============================================================================ grab
def _frame(peak: float | None) -> RtaFrame:
    f = np.geomspace(10.0, 20000.0, 264)
    lvl = np.full(264, -70.0)
    return RtaFrame(f, lvl, lvl.copy(), -90.0, -24.0, peak, "PGM")


def test_grab_is_the_silent_helper_and_never_moves_the_box_selection(tmp_path: Path) -> None:
    async def go() -> None:
        st = State()
        st.live_peq[0].gain = -4.0                       # P1 in use at 250 Hz
        ctl, _ = make_ctl(tmp_path, st)
        await boot(ctl)
        ctl.rta = FakeRta()
        b, s = PhoneBridge(ctl), sess(2)
        ctl.focus.slot = 3                               # the knobs are on P4
        hint = ctl.focus.hint_now()
        # a target: the free bell NEAREST it (P3 at 2.5k for 2.2k), Q 8.65, gain untouched
        ok, v, _why, _c = b.apply2({"t": "grab", "hz": 2200.0, "id": 1}, s)
        assert ok and v["s"] == 2 and v["f"] == 2200.0 and v["g"] == 0.0 and v["on"] is True
        assert v["q"] == pytest.approx(8.651, abs=0.001) and v["new"] is True and v["peak"] is True
        assert ctl.focus.slot == 3 and ctl.focus.hint_now() == hint and ctl.focus.editing_now() is None
        # near a bell in use: that bell, nothing written
        before = ctl.state.to_dict()
        ok, v, _why, _c = b.apply2({"t": "grab", "hz": 260.0}, s)
        assert ok and v["s"] == 0 and v["new"] is False and v["g"] == -4.0
        assert ctl.state.to_dict() == before and ctl.focus.slot == 3
        # the phone's preferred band, when free
        assert b.apply2({"t": "grab", "hz": 4000.0, "s": 1}, s)[1]["s"] == 1
        assert ctl.state.live_peq[1].freq == 4000.0
        # no hz: the box's PEAK (exactly B3), else Q only
        ctl.set_spectrum(_frame(3100.0), "live")
        ok, v, _why, _c = b.apply2({"t": "grab"}, s)
        assert ok and v["peak"] is True and v["f"] == pytest.approx(3100.0, abs=1.0) and v["s"] in (1, 2, 3)
        ctl.set_spectrum(_frame(None), "live")
        ctl.state.live_peq[3].gain = -2.0
        ctl.state.live_peq[1].gain = -2.0
        ctl.state.live_peq[2].gain = -2.0
        ok, v, why, code = b.apply2({"t": "grab", "hz": 8000.0}, s)
        assert (ok, why, code) == (False, "NO FREE BAND", "NOFREE")
        for bad in ({"hz": -5}, {"hz": "2k"}, {"hz": True}, {"s": 6}, {"s": -1}, {"s": True},
                    {"s": 1.0}):
            assert b.apply2({"t": "grab", **bad}, s)[3] == "BAD", bad
        await ctl.stop()
    run(go())


def test_grab_with_a_shelf_selected(tmp_path: Path) -> None:
    """Review S1 #3: with LS or HS selected on the phone (`s` 4 or 5) the long-press notch
    still works: a shelf is never the helper's, so it picks as if no `s` came."""
    async def go() -> None:
        st = State()
        st.live_peq[0].gain = -4.0                       # P1 in use at 250 Hz
        ctl, _ = make_ctl(tmp_path, st)
        await boot(ctl)
        b, p = PhoneBridge(ctl), sess()
        def shelves() -> list[tuple[Any, ...]]:
            return [(sh.freq, sh.gain, sh.q, sh.on)
                    for sh in (ctl.state.live_lowshelf, ctl.state.live_highshelf)]
        before = shelves()
        for s_sel, hz, want in ((4, 2440.0, 2), (5, 5200.0, 3)):
            ok, v, why, code = b.apply2({"t": "grab", "hz": hz, "s": s_sel, "id": 1}, p)
            assert (ok, code) == (True, ""), why
            assert v["s"] == want and v["f"] == hz and v["g"] == 0.0 and v["new"] is True
        assert shelves() == before
        # over the socket: the same, acked with the bell it took
        srv = PhoneServer(ctl, settings(tmp_path), tick_s=0.02)
        await srv.start()
        async with ws_connect(v2url(srv), origin=origin(srv)) as ws:
            await send(ws, t="grab", hz=700.0, s=4, id="g")
            r = await reply(ws, "g")
            assert r["t"] == "ack" and r["v"]["s"] == 1 and r["v"]["f"] == 700.0
        await srv.stop()
        await ctl.stop()
    run(go())


def test_grab_follows_d014_out_rules(tmp_path: Path) -> None:
    async def go() -> None:
        st = State()
        st.live_peq[0].gain, st.live_peq[0].on = -6.0, False     # OUT with a cut: kept, not in use
        st.live_peq[1].on = False                                 # OUT at 0 dB: free
        st.live_peq[2].gain = st.live_peq[3].gain = -3.0          # in use
        ctl, _ = make_ctl(tmp_path, st)
        await boot(ctl)
        b, s = PhoneBridge(ctl), sess()
        ok, v, _why, _c = b.apply2({"t": "grab", "hz": 250.0}, s)   # at P1's frequency: P1 is not "there"
        assert ok and v["s"] == 1 and v["on"] is True and v["f"] == 250.0
        p1 = ctl.state.live_peq[0]
        assert (p1.freq, p1.gain, p1.on) == (250.0, -6.0, False)
        await ctl.stop()
    run(go())


def test_b3_helper_is_unchanged(tmp_path: Path) -> None:
    """select=True (B3) still prefers the box's selected band and moves the box focus."""
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        ctl.focus.slot = 3
        assert ctl.notch_helper() == 3 and ctl.focus.editing_now() == "live_peq.3.gain"
        assert ctl.focus.hint_now().startswith("P4 ·")
        assert ctl.notch_helper(target="x") is None and ctl.notch_helper(target=float("nan")) is None
        await ctl.stop()
    run(go())


# ============================================================================ act, hi, band
class FakeLaptop:
    def __init__(self, refuse: bool = False) -> None:
        self.state, self.refuse, self.on_change = "closed", refuse, None
        self.opens = self.closes = 0

    def pill(self) -> str | None:
        return "LAPTOP OPEN 1:59" if self.state == "open" else None

    def left_s(self) -> float:
        return 7140.2 if self.state == "open" else 0.0

    def open(self) -> bool:
        self.opens += 1
        if self.refuse:
            return False
        self.state = "opening"
        return True

    def close(self, why: str = "box") -> None:
        self.closes += 1
        self.state = "closed"

    def take_message(self) -> str:
        return ""


def test_act_reset_undo_and_laptop(tmp_path: Path) -> None:
    async def go() -> None:
        st = State()
        st.live_peq[1].gain = -5.0
        ctl, _ = make_ctl(tmp_path, st)
        await boot(ctl)
        b, s = PhoneBridge(ctl), sess()
        assert b.apply2({"t": "act", "a": "undo_reset"}, s) == (False, {"undo_s": 0}, "NOTHING TO UNDO", "NOCHANGE")
        ok, v, _w, _c = b.apply2({"t": "act", "a": "reset_live"}, s)
        assert ok and v == {"changed": True, "undo_s": 300} and ctl.state.live_peq[1].gain == 0.0
        assert b.apply2({"t": "act", "a": "reset_live"}, s)[1]["changed"] is False
        assert b.apply2({"t": "reset_live"}, s)[3] == "BAD"                     # v1's form needs confirm
        assert b.apply2({"t": "reset_live", "confirm": True}, s)[0] is True
        assert b.apply2({"t": "act", "a": "undo_reset"}, s)[:2] == (True, {"undo_s": 0})
        assert ctl.state.live_peq[1].gain == -5.0
        assert b.apply2({"t": "act", "a": "laptop_open"}, s)[2:] == ("LAPTOP ACCESS: NOT AVAILABLE", "UNAVAILABLE")
        lap = FakeLaptop()
        ctl.attach_laptop(lap)
        assert b.apply2({"t": "act", "a": "laptop_close"}, s) == (True, {"state": "closed", "s": 0}, "", "")
        assert lap.closes == 0
        assert b.apply2({"t": "act", "a": "laptop_open"}, s)[:2] == (True, {"state": "opening", "s": 0})
        lap.state = "open"
        assert b.apply2({"t": "act", "a": "laptop_open"}, s)[1] == {"state": "open", "s": 7141}
        assert lap.opens == 1
        assert b.apply2({"t": "act", "a": "laptop_close"}, s)[:2] == (True, {"state": "closed", "s": 0})
        assert lap.closes == 1
        lap.refuse = True
        assert b.apply2({"t": "act", "a": "laptop_open"}, s)[3] == "REFUSED"
        assert b.apply2({"t": "act", "a": "shutdown"}, s)[3] == "BAD"
        await ctl.stop()
    run(go())


def test_hi_and_band_on(tmp_path: Path) -> None:
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        b = PhoneBridge(ctl, min_app=3)
        s = sess()
        assert b.apply2({"t": "hi", "app": 2, "bridge": 1, "caps": ["haptics"]}, s)[1] == {"min_app": 3, "app_ok": False}
        assert (s.app, s.bridge, s.caps) == (2, 1, ("haptics",))
        assert b.apply2({"t": "hi", "app": None}, s)[1]["app_ok"] is True
        assert b.apply2({"t": "hi", "app": 3}, s)[1]["app_ok"] is True
        for bad in ({"app": "3"}, {"app": True}, {"caps": "x"}, {"caps": ["x" * 25]}, {"caps": ["a"] * 17}):
            assert b.apply2({"t": "hi", **bad}, s)[3] == "BAD", bad
        # D-014 over v2: the same rule as v1
        assert b.apply2({"t": "band", "s": 1, "on": False}, s) == (True, {"on": False}, "", "")
        assert b.apply2({"t": "band", "s": 1, "on": True, "f": 900.0}, s)[3] == "BAD"
        assert b.apply2({"t": "band", "s": 1, "g": 0, "on": True}, s)[:2] == (True, {"g": 0.0, "on": True})
        assert b.apply2({"t": "band", "s": 9, "g": 0}, s)[3] == "BAD"
        await ctl.stop()
    run(go())


# ============================================================================ mute
def test_mute_is_shared_between_v1_and_v2_and_says_who(tmp_path: Path) -> None:
    async def go() -> None:
        ctl, eng = make_ctl(tmp_path)
        await boot(ctl)
        srv = PhoneServer(ctl, settings(tmp_path, rate_per_s=2.0, burst=2), tick_s=0.02)
        await srv.start()
        async with ws_connect(v1url(srv), origin=origin(srv)) as w1, \
                ws_connect(v2url(srv), origin=origin(srv)) as w2:
            s1 = await recv_until(w1, lambda m: m["t"] == "state")
            assert (await recv_until(w2, lambda m: m["t"] == "hello"))["you"] == 2
            s2 = await recv_until(w2, lambda m: m["t"] == "state")
            assert s1["mute_seq"] == s2["mute_seq"] and s2["mute_src"] == ""
            # v2 mutes (never rate-limited: the bucket is spent first)
            for i in range(4):
                await send(w2, t="nudge", f="balance_db", d=1, id=f"n{i}")
            await send(w2, t="mute", on=True, id="mute")
            assert (await reply(w2, "mute"))["v"] is True
            st2 = await recv_until(w2, lambda m: m["t"] == "state" and m["muted"] is True)
            assert st2["mute_src"] == "phone 2" and st2["hold"] == "unmute"
            # v1's unmute based on its old state: refused, never throttled, with the mute
            for _ in range(2):
                await send(w1, t="mute", on=False, seq=s1["mute_seq"], id="mute")
                e = await reply(w1, "mute")
                assert e["v"] is True and "MUTE CHANGED" in e["why"] and "code" not in e
            # v2's own stale unmute: the same rule, with its code
            await asyncio.sleep(1.1)                      # a token for the unmute (it is limited)
            await send(w2, t="mute", on=False, seq=s2["mute_seq"], id="mute")
            e = await reply(w2, "mute")
            assert e["code"] == "STALE" and e["v"] is True
            await asyncio.sleep(0.6)
            await send(w1, t="mute", on=False, seq=srv.bridge.mute_view()[1], id="mute")
            assert (await reply(w1, "mute"))["v"] is False
            await until(lambda: eng.faders[0]["mute"] is False, what="unmuted")
            st2 = await recv_until(w2, lambda m: m["t"] == "state" and m["muted"] is False)
            assert st2["mute_src"] == ""
            await asyncio.sleep(0.6)
            await send(w2, t="mute", on="yes", id="mute")
            assert (await reply(w2, "mute"))["code"] == "BAD"
        await srv.stop()
        await ctl.stop()
    run(go())


def test_mute_src_in_the_controller(tmp_path: Path) -> None:
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        assert ctl.mute_src == "" and ctl.snapshot()[2].mute_src == ""
        ctl.handle_action(Mute(True))
        assert ctl.mute_src == "B4" and ctl.snapshot()[2].mute_src == "B4"
        ctl.set_mute(False)
        assert ctl.snapshot()[2].mute_src == ""
        ctl.set_mute(True, source="console")
        assert ctl.snapshot()[2].mute_src == "console"
        ctl.set_mute(True)
        assert ctl.mute_src == "local"
        await ctl.stop()
        st = State()
        st.muted = True
        ctl2, _ = make_ctl(tmp_path / "b", st)
        assert ctl2.mute_src == "boot"
    run(go())


# ============================================================================ the envelope
def test_v2_envelope_rate_ids_and_isolation(tmp_path: Path) -> None:
    async def go() -> None:
        ctl, eng = make_ctl(tmp_path)
        await boot(ctl)
        srv = PhoneServer(ctl, settings(tmp_path, rate_per_s=2.0, burst=3), tick_s=0.02)
        await srv.start()
        async with ws_connect(v2url(srv), origin=origin(srv)) as ws:
            await recv_until(ws, lambda m: m["t"] == "state")
            await send(ws, t="ping", ts=5)
            assert (await recv_until(ws, lambda m: m["t"] == "pong"))["ts"] == 5
            await ws.send("not json")
            assert (await recv_until(ws, lambda m: m["t"] == "err"))["code"] == "BAD"
            await ws.send(b"\x00\x01")
            assert (await recv_until(ws, lambda m: m["t"] == "err"))["code"] == "BAD"
            await send(ws, t="set", f="master_db", v=0, id="s")
            assert (await reply(ws, "s"))["code"] == "UNKNOWN"
            for i in range(6):
                await send(ws, t="nudge", f="live_peq.0.gain", d=-1, id=f"g{i}")
            await send(ws, t="mute", on=True, id="mute")
            assert (await reply(ws, "mute"))["v"] is True
            assert srv.stats["rate_limited"] >= 2
            await until(lambda: eng.faders[0]["mute"] is True, what="engine muted")
        await srv.stop()
        await ctl.stop()
    run(go())


def test_a_rate_refused_unmute_is_always_answered(tmp_path: Path) -> None:
    """docs/14 §5 rule 5 (S2 review): an unmute refused by the message bucket gets its err even
    when the error bucket is empty, so the page never says "No answer from K1000" for it."""
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        srv = PhoneServer(ctl, settings(tmp_path, rate_per_s=0.5, burst=1), tick_s=0.02)
        await srv.start()
        async with ws_connect(v2url(srv), origin=origin(srv)) as ws:
            await recv_until(ws, lambda m: m["t"] == "state")
            c = next(x for x in srv._clients if x.v == 2)
            for i in range(4):                       # empty the message bucket
                srv._on_message(c, json.dumps({"t": "nudge", "f": "balance_db", "d": 1, "id": i}))
            if c.errors is not None:                 # and the error bucket
                while c.errors.take():
                    pass
            out = srv._on_message(c, json.dumps({"t": "mute", "on": False, "seen": 0, "id": "um"}))
            assert out is not None and out["t"] == "err" and out["code"] == "RATE" and out["id"] == "um"
            assert out["v"] is srv.bridge._displayed_muted()
        await srv.stop()
        await ctl.stop()
    run(go())


def test_q_steps_halve_and_double() -> None:
    """The v2 stepper for Q / SLOPE reads ×½ · − · + · ×2: ±6 detents of 1/6 octave."""
    from k1000.web import server as srvmod
    assert srvmod._V2_STEPPER[".q"] == (-6, -1, 1, 6)


def test_phone_off_and_max_phones_are_shared(tmp_path: Path) -> None:
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        srv = PhoneServer(ctl, settings(tmp_path, max_clients=2), tick_s=0.02)
        await srv.start()
        async with ws_connect(v1url(srv), origin=origin(srv)) as w1, \
                ws_connect(v2url(srv), origin=origin(srv)) as w2:
            await recv_until(w1, lambda m: m["t"] == "state")
            assert (await recv_until(w2, lambda m: m["t"] == "state"))["phones"] == 2
            async with ws_connect(v2url(srv), origin=origin(srv)) as w3:
                bye = json.loads(await w3.recv())
                assert bye["t"] == "bye" and bye["code"] == 4002
            c2 = next(c for c in srv._clients if c.v == 2)
            srv._on = False
            assert srv._on_message(c2, json.dumps({"t": "nudge", "f": "balance_db", "d": 1, "id": 1}))["code"] == "OFF"
            srv._on = True
            srv.set_enabled(False)
            with pytest.raises(ConnectionClosed):
                while True:
                    await asyncio.wait_for(w2.recv(), 2.0)
            assert w2.close_code == 4003
        srv.set_enabled(True)
        await srv.stop()
        await ctl.stop()
    run(go())


# ============================================================================ the spectrum
def test_rta_message_encoding() -> None:
    f = np.geomspace(10.0, 20000.0, 264)
    lvl = np.linspace(-24.0, -200.0, 264)
    hold = lvl + 3.0
    fr = RtaFrame(f, lvl, hold, -90.0, -24.04, 2440.25, "PGM")
    m = rta_message(fr, "live", 7)
    json.dumps(m, allow_nan=False)
    assert (m["t"], m["seq"], m["src"], m["st"], m["n"]) == ("rta", 7, "PGM", "live", 264)
    assert (m["f0"], m["f1"], m["hi"], m["lo"], m["peak_hz"]) == (10.0, 20000.0, -24.0, -90.0, 2440.2)
    b = base64.b64decode(m["b"])
    assert len(b) == 264 and b[0] == 0 and b[-1] == 255
    for i in (0, 50, 100):
        assert m["hi"] - b[i] * 0.5 == pytest.approx(lvl[i], abs=0.25)
    h = base64.b64decode(m["h"])
    assert len(h) == 264 and m["hi"] - h[50] * 0.5 == pytest.approx(hold[50], abs=0.25)
    assert len(m["b"]) < 400 and len(json.dumps(m)) < 1000
    empty = rta_message(None, "waiting", 8)
    assert empty["st"] == "waiting" and empty["b"] is None and empty["n"] == 0 and empty["hi"] is None
    odd = rta_message(RtaFrame(f, lvl, None, -90.0, float("nan"), None), "live", 9)
    assert odd["b"] is None
    lvl2 = lvl.copy()
    lvl2[3] = float("nan")
    assert base64.b64decode(rta_message(RtaFrame(f, lvl2, None, -90.0, -24.0), "live", 1)["b"])[3] == 255


def test_spectrum_wanted_with_phones_and_headless(tmp_path: Path) -> None:
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        await ctl.poll_once()
        assert ctl.page == "SHOW" and ctl.spectrum_wanted() is True
        ctl.has_screen = False                           # --headless: SHOW alone is nobody
        assert ctl.spectrum_wanted() is False
        ctl.set_spectrum_phones(1)
        assert ctl.spectrum_wanted() is True
        ctl.goto_page("ALIGN")
        assert ctl.spectrum_wanted() is True
        ctl.state.spectrum_on = False                    # SETUP > Spectrum OFF still stops it
        assert ctl.spectrum_wanted() is False
        ctl.state.spectrum_on = True
        ctl.set_spectrum_phones(0)
        assert ctl.spectrum_wanted() is False
        await ctl.stop()
    run(go())


def test_headless_box_opens_the_spectrum_but_connects_only_for_a_phone(tmp_path: Path,
                                                                        monkeypatch: Any) -> None:
    """--headless --no-hw: the spectrum task exists (spectrum.enabled), its own engine link is
    opened only once a phone subscribes; hello.surface says what the box has."""
    from k1000 import __main__ as main_mod
    made: list[str] = []

    class OwnClient:
        def __init__(self, url: str, **kw: Any) -> None:
            made.append(url)
            self.connected = False

        async def connect(self, timeout: Any = None) -> bool:
            return False

        async def close(self) -> None:
            pass

    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        main_mod.wire_surfaces(ctl, main_mod.build_parser().parse_args(["--headless", "--no-hw"]))
        assert ctl.has_screen is False and ctl.has_knobs is False
        await boot(ctl)
        await ctl.poll_once()
        monkeypatch.setattr(main_mod, "CamillaClient", OwnClient)
        task = main_mod.open_spectrum(ctl, "ws://127.0.0.1:4321")
        assert task is not None and ctl.page == "SHOW"
        ctl.rta = task
        await asyncio.sleep(0.25)
        assert made == []                                 # nobody looks: no second link
        srv = PhoneServer(ctl, settings(tmp_path), tick_s=0.02)
        await srv.start()
        async with ws_connect(v2url(srv), origin=origin(srv)) as ws:
            h = json.loads(await ws.recv())
            assert h["surface"] == {"screen": False, "knobs": False}
            st = await recv_until(ws, lambda m: m["t"] == "state")
            assert st["mute_fallback"] == "turn the source down · bypass"
            await send(ws, t="sub", rta=True, id=1)
            await reply(ws, 1)
            await until(lambda: made == ["ws://127.0.0.1:4321"], what="the spectrum link")
            assert (await recv_until(ws, lambda m: m["t"] == "rta" and m["st"] == "waiting"))["b"] is None
        await srv.stop()
        await task.stop()
        await ctl.stop()
    run(go())


def test_rta_subscribe_frames_rate_and_unsubscribe(tmp_path: Path) -> None:
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        await ctl.poll_once()
        ctl.rta = FakeRta()
        ctl.goto_page("ALIGN")
        srv = PhoneServer(ctl, settings(tmp_path), tick_s=0.02)
        await srv.start()
        assert srv._on_spectrum in ctl.spectrum_hooks
        async with ws_connect(v2url(srv), origin=origin(srv)) as ws, \
                ws_connect(v2url(srv), origin=origin(srv)) as quiet:
            await recv_until(ws, lambda m: m["t"] == "state")
            assert ctl.spectrum_wanted() is False
            await send(ws, t="sub", rta="yes", id=0)
            assert (await reply(ws, 0))["code"] == "BAD"
            await send(ws, t="sub", rta=True, id=1)
            ack = await reply(ws, 1)
            assert ack["v"]["rta"] is True
            assert ctl.spectrum_phones == 1 and ctl.spectrum_wanted() is True
            first = await recv_until(ws, lambda m: m["t"] == "rta")
            assert first["b"] is None                    # nothing published yet
            # a burst of frames: at most one per 100 ms reaches the phone, the latest last
            t0 = time.monotonic()
            for k in range(12):
                ctl.set_spectrum(_frame(1000.0 + k), "live")
                await asyncio.sleep(0.01)
            got: list[dict[str, Any]] = []
            while True:
                try:
                    m = json.loads(await asyncio.wait_for(ws.recv(), 0.4))
                except (TimeoutError, asyncio.TimeoutError):
                    break
                if m["t"] == "rta":
                    got.append(m)
            span = time.monotonic() - t0
            assert got and got[-1]["peak_hz"] == 1011.0 and got[-1]["st"] == "live"
            assert len(got) <= span / web.RTA_MIN_INTERVAL_S + 2
            assert base64.b64decode(got[-1]["b"])[0] == round((-24.0 + 70.0) / 0.5)
            # the other phone never asked: no frame
            with pytest.raises((TimeoutError, asyncio.TimeoutError)):
                await recv_until_t(quiet, "rta", 0.3)
            await send(ws, t="sub", rta=False, id=2)
            assert (await reply(ws, 2))["v"]["rta"] is False
            assert ctl.spectrum_phones == 0 and ctl.spectrum_wanted() is False
            ctl.set_spectrum(_frame(2000.0), "live")
            with pytest.raises((TimeoutError, asyncio.TimeoutError)):
                await recv_until_t(ws, "rta", 0.3)
            await send(ws, t="sub", rta=True, id=3)
            await reply(ws, 3)
            assert (await recv_until(ws, lambda m: m["t"] == "rta"))["peak_hz"] == 2000.0
        await until(lambda: srv.phones == 0, what="phones gone")
        assert ctl.spectrum_phones == 0                  # a disconnect unsubscribes
        await srv.stop()
        assert srv._on_spectrum not in ctl.spectrum_hooks
        await ctl.stop()
    run(go())


async def recv_until_t(ws: Any, t: str, timeout: float) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(), max(0.001, end - loop.time())))
        if m.get("t") == t:
            return m


class _RecWS:
    """A phone socket that records what is sent and reports a write backlog the test sets."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.backlog = 0
        self.remote_address = ("10.42.0.9", 1)
        outer = self

        class _T:
            def get_write_buffer_size(self) -> int:
                return outer.backlog

            def abort(self) -> None:
                pass
        self.transport = _T()

    async def send(self, text: str) -> None:
        self.sent.append(json.loads(text))


def test_rta_is_dropped_first_and_never_ahead_of_a_reply(tmp_path: Path) -> None:
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        ctl.rta = FakeRta()
        srv = PhoneServer(ctl, settings(tmp_path))
        ctl.spectrum_hooks.append(srv._on_spectrum)    # what start() does
        ws = _RecWS()
        c = web._Client(ws, "10.42.0.9", web._Bucket(30.0, 40, time.monotonic), v=2, n=1,
                        sess=PhoneSession(n=1, rta=True))
        srv._clients.add(c)
        srv._refresh(force=True)
        ctl.set_spectrum(_frame(1500.0), "live")       # the hook encodes a frame
        assert srv._rta_json and '"rta"' in srv._rta_json
        srv._queue(c, {"t": "ack", "id": "mute", "v": True})
        task = asyncio.get_running_loop().create_task(srv._sender2(c))
        await until(lambda: any(m["t"] == "rta" for m in ws.sent), what="frame sent")
        order = [m["t"] for m in ws.sent]
        assert order.index("ack") < order.index("state") < order.index("m") < order.index("rta")
        # backpressure: the socket still holds > 4 kB -> the frame is dropped, not queued
        ws.backlog = 10_000
        n_rta = sum(1 for m in ws.sent if m["t"] == "rta")
        srv._rta_t = -math.inf
        ctl.set_spectrum(_frame(1600.0), "live")
        await until(lambda: srv.stats["rta_dropped"] >= 1, what="frame dropped")
        assert sum(1 for m in ws.sent if m["t"] == "rta") == n_rta
        # ...while a reply still goes out at once
        srv._queue(c, {"t": "ack", "id": "mute", "v": False})
        await until(lambda: ws.sent[-1] == {"t": "ack", "id": "mute", "v": False}, what="ack sent")
        ws.backlog = 0
        srv._rta_t = -math.inf
        ctl.set_spectrum(_frame(1700.0), "live")
        await until(lambda: ws.sent[-1].get("peak_hz") == 1700.0, what="next frame")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await ctl.stop()
    run(go())


def _masked(text: str) -> bytes:
    """One client text frame (masked, as a browser sends it)."""
    data = text.encode()
    mask = os.urandom(4)
    n = len(data)
    head = bytes([0x81, 0x80 | n]) if n < 126 else bytes([0x81, 0x80 | 126]) + struct.pack("!H", n)
    return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data))


def _stalled_phone(port: int, ctx: ssl.SSLContext | None, org: str, ready: threading.Event,
                   stop: threading.Event) -> None:
    """(a thread) A v2 phone that opens /v2/ws (TLS or plain), subscribes to the spectrum,
    reads what the box has sent so far (hello, state, the first frame) and then never reads
    again: a weak Wi-Fi link or a frozen page. Its receive buffer is tiny, so what the box
    writes next piles up on the box's side (in the kernel's send queue first)."""
    raw = socket.socket()
    raw.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2048)
    raw.connect(("127.0.0.1", port))
    sock = ctx.wrap_socket(raw, server_hostname="localhost") if ctx is not None else raw
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((f"GET /v2/ws HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                      f"Sec-WebSocket-Version: 13\r\nOrigin: {org}\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += sock.recv(1)
        assert head.startswith(b"HTTP/1.1 101"), head[:40]
        sock.sendall(_masked(json.dumps({"t": "sub", "rta": True})))
        sock.settimeout(0.3)
        with contextlib.suppress(TimeoutError, socket.timeout):
            while sock.recv(65536):                      # drain, then stall
                pass
        ready.set()
        stop.wait(20.0)
    finally:
        sock.close()


@pytest.mark.parametrize("tls", [False, True], ids=["plain", "tls"])
def test_backlog_counts_what_waits_in_the_kernel(tmp_path: Path, tls: bool) -> None:
    """Review S1 #2: asyncio's buffer stays at 0 while the kernel holds the bytes (megabytes on
    a stalled link, TLS or plain); the drop rule must see those. Over the real server: a phone
    that stops reading gets a few frames, then they are dropped instead of piling up ahead of
    a later mute ack."""
    cert = _self_signed(tmp_path / "tls") if tls else None
    if tls and cert is None:
        pytest.skip("openssl CLI not available to make a throwaway certificate")

    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        ctl.rta = FakeRta()
        if cert is not None:
            cfg = settings(tmp_path, https_port=free_port(), http_port=free_port(),
                           plain_app="fallback", tls_dir=cert)
        else:
            cfg = settings(tmp_path)
        srv = PhoneServer(ctl, cfg, tick_s=0.02)
        await srv.start()
        port = srv.ports["https" if tls else "plain"]
        ctx = ssl.create_default_context(cafile=str(cert / "k1000.crt")) if cert is not None else None
        org = f"https://localhost:{port}" if tls else origin(srv)
        ready, stop = threading.Event(), threading.Event()
        th = threading.Thread(target=_stalled_phone, args=(port, ctx, org, ready, stop), daemon=True)
        th.start()
        try:
            await until(ready.is_set, what="the phone stalled")
            assert ctl.spectrum_phones == 1
            (c,) = list(srv._clients)
            frame_len = 0
            for k in range(200):                         # ~170 kB of frames offered
                srv._rta_t = -math.inf
                ctl.set_spectrum(_frame(1000.0 + k), "live")
                frame_len = max(frame_len, len(srv._rta_json))
                await asyncio.sleep(0.003)
            await asyncio.sleep(0.1)
            sent, dropped = srv.stats["rta_sent"], srv.stats["rta_dropped"]
            assert sent > 1 and dropped > 0, (sent, dropped)      # a few frames, then drops
            # what was written is bounded by the phone's small window plus RTA_MAX_BUFFERED,
            # not by the kernel's send buffer (before the fix: all 200 frames went in)
            assert sent * frame_len < 64 * 1024, (sent, frame_len)
            bl = srv._backlog(c)
            asyncio_only = c.ws.transport.get_write_buffer_size()
            assert bl > web.RTA_MAX_BUFFERED and asyncio_only < bl
        finally:
            stop.set()
            th.join(5.0)
        await srv.stop()
        await ctl.stop()
    run(go())


def test_backlog_unit_kernel_queue() -> None:
    """_backlog reads the kernel's send queue (TIOCOUTQ) of the transport's socket and adds the
    asyncio buffers; a stand-in without a socket counts what its transport says."""
    srv_sock = socket.socket()
    srv_sock.bind(("127.0.0.1", 0))
    srv_sock.listen(1)
    rx = socket.socket()
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2048)
    rx.connect(srv_sock.getsockname())
    tx, _ = srv_sock.accept()
    tx.setblocking(False)
    try:
        sent = 0
        with pytest.raises(BlockingIOError):
            while sent < 50_000_000:
                sent += tx.send(b"x" * 65536)

        class T:
            def get_write_buffer_size(self) -> int:
                return 100

            def get_extra_info(self, name: str, default: Any = None) -> Any:
                return tx if name == "socket" else default

        class W:
            transport = T()
        bl = PhoneServer._backlog(web._Client(W(), "x", web._Bucket(1, 1, time.monotonic)))
        assert bl > 100 + web.RTA_MAX_BUFFERED and bl <= sent + 100
        assert PhoneServer._backlog(web._Client(_RecWS(), "x", web._Bucket(1, 1, time.monotonic))) == 0
    finally:
        for x in (tx, rx, srv_sock):
            x.close()


def test_rta_subscription_lapses_when_the_page_goes_quiet(tmp_path: Path) -> None:
    """Review S1 #5: a frozen page keeps its socket (the browser answers the box's websocket
    pings) but its own pings stop: after RTA_SUB_IDLE_S without a message the subscription
    lapses, the phone is told, and the box stops polling the spectrum for it. Any message
    (the 2 s ping) keeps it."""
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        await ctl.poll_once()
        ctl.rta = FakeRta()
        ctl.goto_page("ALIGN")
        clk = Clock(time.monotonic())
        srv = PhoneServer(ctl, settings(tmp_path), tick_s=0.02, clock=clk)
        await srv.start()
        idle = web.RTA_SUB_IDLE_S
        async with ws_connect(v2url(srv), origin=origin(srv)) as ws:
            assert json.loads(await ws.recv())["rta"]["idle_s"] == idle
            await send(ws, t="sub", rta=True, id=1)
            await reply(ws, 1)
            assert ctl.spectrum_wanted() is True
            # pings keep it, however long
            for _ in range(4):
                clk.t += idle * 0.8
                await send(ws, t="ping", ts=1)
                await recv_until(ws, lambda m: m["t"] == "pong")
            await asyncio.sleep(0.1)
            assert ctl.spectrum_phones == 1
            # silence: it lapses, said once
            clk.t += idle + 0.5
            note = await recv_until(ws, lambda m: m["t"] == "unsub")
            assert note == {"t": "unsub", "what": "rta", "why": "idle"}
            assert ctl.spectrum_phones == 0 and ctl.spectrum_wanted() is False
            srv._rta_t = -math.inf
            ctl.set_spectrum(_frame(1234.0), "live")
            with pytest.raises((TimeoutError, asyncio.TimeoutError)):
                await recv_until_t(ws, "rta", 0.3)
            with pytest.raises((TimeoutError, asyncio.TimeoutError)):
                await recv_until_t(ws, "unsub", 0.2)
            # the page (visible again) subscribes again
            await send(ws, t="sub", rta=True, id=2)
            await reply(ws, 2)
            assert ctl.spectrum_phones == 1
            assert (await recv_until(ws, lambda m: m["t"] == "rta"))["peak_hz"] == 1234.0
        await srv.stop()
        await ctl.stop()
    run(go())


def test_spectrum_frames_wake_the_screen_only_on_show(tmp_path: Path) -> None:
    """Review S1 #6: a phone subscribed while the box shows ALIGN / SETUP / PHONE: each frame
    reaches the phones' hook, but the box screen is not re-rendered at the frame rate."""
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        await ctl.poll_once()
        hooked: list[int] = []
        ctl.spectrum_hooks.append(lambda: hooked.append(1))
        ctl.set_spectrum_phones(1)
        k = 0
        for page in ("ALIGN", "SETUP", "PHONE"):
            ctl.goto_page(page)
            ctl.changed.clear()
            v = ctl.version
            for _ in range(3):
                k += 1
                ctl.set_spectrum(_frame(1000.0 + k), "live")
            assert not ctl.changed.is_set(), page
            assert ctl.version == v
        assert len(hooked) == k
        ctl.goto_page("SHOW")
        ctl.changed.clear()
        ctl.set_spectrum(_frame(4000.0), "live")
        assert ctl.changed.is_set() and len(hooked) == k + 1
        ctl.has_screen = False                           # --headless: no screen to wake
        ctl.changed.clear()
        ctl.set_spectrum(_frame(4100.0), "live")
        assert not ctl.changed.is_set() and len(hooked) == k + 2
        await ctl.stop()
    run(go())


def test_v1_worker_never_answers_for_v2() -> None:
    """Review S1 #1: v1's worker (scope "/") answers every navigation in its scope with its
    cached "/": unless it skips /v2/, a browser or an APK WebView that ever loaded "/" shows
    v1 at /v2/. Its fetch handler's first early return must leave /v2/ to the network (and
    to v2's own worker)."""
    sw = (web.STATIC_DIR / "sw.js").read_text()
    handler = sw[sw.index('addEventListener("fetch"'):]
    first_return = handler[:handler.index("respondWith")]
    assert re.search(r'url\.pathname\.startsWith\(\s*"/v2/"\s*\)', first_return), first_return


def test_input_meter_moves_without_bumping_version(tmp_path: Path) -> None:
    """The capture peaks reach v2's `m.i` through Controller.meter_seq, not `version` (since D-015 they do wake the box screen)."""
    async def go() -> None:
        ctl, eng = make_ctl(tmp_path)
        await boot(ctl)
        caps = [-100.0, -100.0, -100.0]
        real = eng.command

        async def command(name: str, timeout: float | None = None, **kw: Any) -> Any:
            if name == "GetCaptureSignalPeakSinceLast":
                return list(caps)
            return await real(name, timeout, **kw)
        eng.command = command                            # type: ignore[method-assign]
        await ctl.meter_once()
        v, ms = ctl.version, ctl.meter_seq
        caps[0], caps[1] = -18.0, -21.0                  # dev input_map (0, 1)
        await ctl.meter_once()
        assert ctl.version == v and ctl.meter_seq > ms
        assert ctl.status.in_peak_db == [-18.0, -21.0]
        srv = PhoneServer(ctl, settings(tmp_path), tick_s=0.02)
        await srv.start()
        async with ws_connect(v2url(srv), origin=origin(srv)) as ws:
            m = await recv_until(ws, lambda m: m["t"] == "m")
            assert m["i"] == [-18.0, -21.0]
            caps[0] = -6.0
            await ctl.meter_once()
            assert (await recv_until(ws, lambda m: m["t"] == "m" and m["i"][0] == -6.0))["i"] == [-6.0, -21.0]
        await srv.stop()
        await ctl.stop()
    run(go())


def test_several_v2_phones_share_values_not_locks(tmp_path: Path) -> None:
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        srv = PhoneServer(ctl, settings(tmp_path), tick_s=0.02)
        await srv.start()
        async with ws_connect(v2url(srv), origin=origin(srv)) as a, \
                ws_connect(v2url(srv), origin=origin(srv)) as b, \
                ws_connect(v1url(srv), origin=origin(srv)) as c:
            sc = await recv_until(c, lambda m: m["t"] == "state" and m["phones"] == 3)
            assert "align_delay_ms" not in sc and "setup" not in sc            # v1 state stays v1
            await send(a, t="choose", f="top_inverted", v=True, seen=False, id=1)
            assert (await reply(a, 1))["v"] is True
            sb = await recv_until(b, lambda m: m["t"] == "state" and m["setup"]["top_inverted"] is True)
            assert "POL INV TOP" in sb["pills"]
            await send(b, t="choose", f="top_inverted", v=False, seen=False, id=2)   # b looked before
            e = await reply(b, 2)
            assert e["code"] == "STALE" and e["v"] is True
            await send(a, t="unlock", what="align", id=3)
            await reply(a, 3)
            await send(b, t="nudge", f="align_delay_ms", d=1, id=4)
            assert (await reply(b, 4))["code"] == "LOCKED"
            await send(a, t="nudge", f="align_delay_ms", d=1, id=5)
            assert (await reply(a, 5))["v"] == 0.1
        await srv.stop()
        await ctl.stop()
    run(go())
