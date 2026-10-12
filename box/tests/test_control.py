# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for k1000.control (the Controller) and k1000.__main__ (the service wiring).

Two layers:
* unit tests against `FakeClient`, an in-memory engine with the CamillaClient surface the
  controller uses: ops emitted per action, clamping, the balance macro, the boot ramp, mute
  priority, setup-action refusal, adoption of external edits, reconnect rules, persistence;
* `engine` tests against a real camilladsp (SignalGenerator -> File /dev/null, never the ALSA
  card) on the private port $K1000_CONTROL_TEST_PORT (default 12346): the controller in-process through a scripted show, an engine
  kill + restart (muted restart -> ramp), and `python3 -m k1000` as a subprocess killed and
  restarted while the engine keeps playing unchanged.

No pytest-asyncio on the box (apt only): each test drives its own asyncio.run().
"""
from __future__ import annotations

import asyncio
import copy
import inspect
import json
import math
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from k1000 import control as ctl_mod
from k1000 import dspconfig as dc
from k1000.cdsp import CamillaClient, CdspError, CdspNoConfig, CdspUnavailable
from k1000.control import (
    MARKER_DB, MARKER_FADER, Controller, EngineSettings, Paths, Status, decode_marker, logical_levels,
    marker_db, recover_state,
)
from k1000.hw.mcp23017 import EncoderEvent, SwitchEvent
from k1000.profiles import ProfileChoice, load_profile
from k1000.state import RANGES, State, delay_samples

BOX = Path(__file__).resolve().parents[1]
CDSP_BIN = "/usr/local/bin/camilladsp"
ENGINE_PORT = int(os.environ.get("K1000_CONTROL_TEST_PORT", "12346"))


def run(coro: Any, limit: float = 30.0) -> Any:
    return asyncio.run(asyncio.wait_for(coro, limit))


async def until(pred: Any, timeout: float = 3.0, what: str = "condition", step: float = 0.005) -> None:
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
        await asyncio.sleep(step)


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


def fresh_faders() -> list[dict[str, Any]]:
    """camilladsp -m -g-40 at start (measured on the box): Main -40 muted, Aux 0 dB unmuted."""
    return [{"volume": -40.0, "mute": True}] + [{"volume": 0.0, "mute": False} for _ in range(4)]


class FakeClient:
    """The CamillaClient surface the controller uses, over an in-memory engine. `log` records
    every write as ("set", ptr, v) / ("patch", dict) / ("config", dict) / ("fader", i, db) /
    ("mute", i, bool) in order."""

    def __init__(self, cfg: dict[str, Any] | None) -> None:
        self.cfg = copy.deepcopy(cfg)
        self.faders = fresh_faders()
        self.connected = False
        self.on_connect: Any = None
        self.on_disconnect: Any = None
        self.log: list[tuple[Any, ...]] = []
        self.engine_state = "Running"
        self.peaks: list[float] = [-20.0, -20.0]
        self.clipped = 0
        self.refuse: set[str] = set()
        self.hooks: list[asyncio.Task[Any]] = []
        self.on_write: Any = None       # optional async callback(entry) after each write
        self.cap_peaks: list[float] | None = None   # GetCaptureSignalPeakSinceLast (None: unknown)

    # -- lifecycle
    def _fire(self, hook: Any) -> None:
        if hook is not None:
            self.hooks.append(asyncio.get_running_loop().create_task(hook(self)))

    async def connect(self, timeout: float | None = None) -> bool:
        if not self.connected:
            self.connected = True
            self._fire(self.on_connect)
        return True

    async def wait_connected(self, timeout: float | None = None) -> bool:
        return self.connected

    async def drop(self) -> None:
        self.connected = False
        for t in self.hooks:
            t.cancel()
        self._fire(self.on_disconnect)
        await asyncio.sleep(0)

    async def restart(self, cfg: dict[str, Any] | None) -> None:
        """Engine killed and started again from a file: fresh faders, that config."""
        await self.drop()
        self.cfg = copy.deepcopy(cfg)
        self.faders = fresh_faders()
        self.connected = True
        self._fire(self.on_connect)

    async def reconnect(self) -> None:
        self.connected = True
        self._fire(self.on_connect)

    async def close(self) -> None:
        self.connected = False

    def _need(self) -> None:
        if not self.connected:
            raise CdspUnavailable("fake engine down")

    async def _logged(self, entry: tuple[Any, ...]) -> None:
        self.log.append(entry)
        if self.on_write is not None:
            await self.on_write(entry)

    # -- reads
    async def get_version(self) -> str:
        self._need()
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
        return self.engine_state

    async def get_processing_load(self) -> float:
        self._need()
        return 1.5

    async def get_clipped_samples(self) -> int:
        self._need()
        return self.clipped

    async def get_stop_reason(self) -> str:
        return "None"

    async def command(self, name: str, timeout: float | None = None, **fields: Any) -> Any:
        self._need()
        if name == "GetPlaybackSignalPeakSinceLast":
            return list(self.peaks)
        if name == "GetCaptureSignalPeakSinceLast" and self.cap_peaks is not None:
            return list(self.cap_peaks)
        raise CdspError(name, "Invalid", "fake: unknown")

    # -- writes
    async def set_value(self, pointer: str, value: Any, clamp: Any = None) -> Any:
        self._need()
        if pointer in self.refuse:
            raise CdspError("SetConfigValue", "ConfigValidationError", "fake refusal")
        _set_ptr(self.cfg, pointer, value)
        await self._logged(("set", pointer, value))
        return value

    async def patch_config(self, patch: dict[str, Any]) -> None:
        self._need()
        _merge(self.cfg, patch)
        await self._logged(("patch", copy.deepcopy(patch)))

    async def set_config_yaml(self, text: str, timeout: float = 10.0) -> None:
        self._need()
        self.cfg = yaml.safe_load(text)
        await self._logged(("config", copy.deepcopy(self.cfg)))

    async def set_fader(self, idx: int, db: float) -> float:
        self._need()
        self.faders[idx]["volume"] = db
        await self._logged(("fader", idx, db))
        return db

    async def set_fader_mute(self, idx: int, mute: bool) -> None:
        self._need()
        self.faders[idx]["mute"] = bool(mute)
        await self._logged(("mute", idx, bool(mute)))


async def fast_sleep(s: float) -> None:
    await asyncio.sleep(0)


DEV = load_profile("dev", {})


def make(tmp_path: Path, state: State | None = None, *, playing: bool = False,
         cfg: dict[str, Any] | None | str = "expected", **kw: Any) -> tuple[Controller, FakeClient]:
    """A controller over a fake engine. `playing` = an engine K1000 already brought in (marker
    set, Main unmuted at the state's master); default = a freshly started engine."""
    st = state.copy() if state is not None else State()
    engine_cfg = dc.generate(DEV, st) if cfg == "expected" else cfg
    client = FakeClient(engine_cfg)  # type: ignore[arg-type]
    if playing:
        lv = dc.fader_levels(st)
        client.faders[0] = {"volume": lv[0], "mute": st.muted}
        client.faders[1]["volume"], client.faders[2]["volume"] = lv[1], lv[2]
        client.faders[MARKER_FADER]["volume"] = MARKER_DB
    opts: dict[str, Any] = dict(sleep=fast_sleep, poll_s=3600, meter_s=3600,
                                persist_debounce_s=0.05, persist_max_delay_s=0.5,
                                device_probe=lambda d: True)
    opts.update(kw)
    ctl = Controller(client, DEV, st, Paths(tmp_path / "state.json", tmp_path / "active.yml"), **opts)
    return ctl, client


async def boot(ctl: Controller) -> None:
    await ctl.start()
    await until(lambda: ctl.engine_ready, what="connect sync")
    assert await ctl.settle(timeout=3.0)


def nomark(log: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    """The log without K1000's marker writes (Aux4: guard records, see control.marker_db)."""
    return [e for e in log if not (e[0] == "fader" and e[1] == MARKER_FADER)]


def faders_sent(log: list[tuple[Any, ...]], idx: int) -> list[float]:
    return [e[2] for e in log if e[0] == "fader" and e[1] == idx]


def sets_for(log: list[tuple[Any, ...]], pointer: str) -> list[Any]:
    """Values written to `pointer`, by SetConfigValue or inside a PatchConfig."""
    out = []
    parts = pointer.strip("/").split("/")
    for e in log:
        if e[0] == "set" and e[1] == pointer:
            out.append(e[2])
        elif e[0] == "patch":
            node: Any = e[1]
            for p in parts:
                node = node.get(p) if isinstance(node, dict) else None
            if node is not None:
                out.append(node)
    return out


# ======================================================================= boot / reconnect
def test_fresh_engine_boot_ramp_is_gradual_and_unmutes_low(tmp_path: Path) -> None:
    async def body() -> None:
        st = State(master_db=0.0, balance_db=2.0)
        ctl, cl = make(tmp_path, st)
        await boot(ctl)
        log = cl.log
        unmute_at = log.index(("mute", 0, False))
        before = log[:unmute_at]
        # Aux levels + Aux unmutes, Main parked at the boot gain, all BEFORE the Main unmute
        assert ("fader", 1, 1.0) in before and ("fader", 2, -1.0) in before
        assert ("mute", 1, False) in before and ("mute", 2, False) in before
        assert faders_sent(before, 0) == [-40.0]
        # then Main climbs to the master in small steps, never down, never a jump
        steps = [-40.0] + faders_sent(log[unmute_at:], 0)
        assert steps[-1] == 0.0
        deltas = [b - a for a, b in zip(steps, steps[1:])]
        assert all(0 < d <= 6.0 for d in deltas), deltas
        assert len(deltas) >= 5
        # the marker is set once the engine is ours; the engine ends unmuted at the master
        assert log[-1] == ("fader", MARKER_FADER, MARKER_DB)
        assert cl.faders[0] == {"volume": 0.0, "mute": False}
        assert ctl.status.muted is False and not ctl.ramping
        await ctl.stop()

    run(body())


def test_fresh_engine_with_saved_mute_stays_muted(tmp_path: Path) -> None:
    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-10.0, muted=True))
        await boot(ctl)
        assert ("mute", 0, False) not in cl.log
        assert cl.faders[0] == {"volume": -10.0, "mute": True}
        assert ctl.displayed_muted()
        await ctl.stop()

    run(body())


def test_fresh_engine_gets_the_saved_state_before_the_unmute(tmp_path: Path) -> None:
    """active.yml older than state.json (or another config): pushed while still muted."""
    async def body() -> None:
        st = State()
        st.live_peq[0].gain = -6.0
        stale = dc.generate(DEV, State())        # the engine booted a config without that cut
        ctl, cl = make(tmp_path, st, cfg=stale)
        await boot(ctl)
        unmute_at = cl.log.index(("mute", 0, False))
        assert sets_for(cl.log[:unmute_at], "/filters/live_peq_1/parameters/gain") == [-6.0]
        # a foreign title is replaced whole, also before the unmute
        other = dc.generate(DEV, st)
        other["title"] = "someone else's"
        ctl2, cl2 = make(tmp_path / "b", st, cfg=other)
        await boot(ctl2)
        kinds = [e[0] for e in cl2.log]
        assert kinds.index("config") < cl2.log.index(("mute", 0, False))
        await ctl.stop()
        await ctl2.stop()

    run(body())


def test_engine_without_config_gets_one(tmp_path: Path) -> None:
    async def body() -> None:
        ctl, cl = make(tmp_path, State(), cfg=None)
        await boot(ctl)
        assert cl.cfg is not None and cl.cfg["title"] == "K1000 dev"
        await ctl.stop()

    run(body())


def test_reconnect_to_a_playing_engine_adopts_and_never_ramps(tmp_path: Path) -> None:
    """A k1000 restart while the engine plays: no drop to -40, no mute, the engine's levels win."""
    async def body() -> None:
        engine_state = State(master_db=-12.0, balance_db=3.0)
        engine_state.live_peq[2].gain = -4.5
        saved = State(master_db=-30.0)      # state.json older than what the engine plays
        ctl, cl = make(tmp_path, saved, cfg=dc.generate(DEV, engine_state))
        lv = dc.fader_levels(engine_state)
        cl.faders[0] = {"volume": -12.0, "mute": False}
        cl.faders[1]["volume"], cl.faders[2]["volume"] = lv[1], lv[2]
        cl.faders[MARKER_FADER]["volume"] = MARKER_DB
        await boot(ctl)
        assert not any(e[0] in ("fader", "mute") and e[1] == 0 for e in cl.log), cl.log
        assert not any(e[0] in ("config", "set", "patch") for e in cl.log), cl.log
        assert ctl.state.master_db == -12.0 and ctl.state.balance_db == 3.0
        assert ctl.state.live_peq[2].gain == -4.5
        assert cl.faders[0] == {"volume": -12.0, "mute": False}
        await ctl.stop()
        saved_now = json.loads((tmp_path / "state.json").read_text())
        assert saved_now["master_db"] == -12.0 and saved_now["live_peq"][2]["gain"] == -4.5

    run(body())


def test_playing_engine_without_marker_keeps_the_saved_balance(tmp_path: Path) -> None:
    """Someone unmuted a fresh engine before K1000 synced: Aux 0/0 are engine defaults, not a
    balance. The saved balance is pushed; Main is adopted as it plays (never raised)."""
    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-10.0, balance_db=4.0))
        cl.faders[0] = {"volume": -40.0, "mute": False}       # unmuted by the phone, no marker
        await boot(ctl)
        assert ctl.state.balance_db == 4.0
        assert cl.faders[1]["volume"] == 2.0 and cl.faders[2]["volume"] == -2.0
        assert ctl.state.master_db == -40.0 and cl.faders[0] == {"volume": -40.0, "mute": False}
        assert not any(e[0] == "fader" and e[1] == 0 for e in cl.log)
        assert cl.faders[MARKER_FADER]["volume"] == MARKER_DB
        await ctl.stop()
        # ...but Aux faders someone already MOVED on that engine are theirs: never raised
        ctl2, cl2 = make(tmp_path / "b", State(master_db=-10.0, balance_db=4.0))
        cl2.faders[0] = {"volume": -18.0, "mute": False}
        cl2.faders[2]["volume"] = -9.0                         # tops pulled down from the phone
        await boot(ctl2)
        assert cl2.faders[2]["volume"] <= -9.0 and cl2.faders[1]["volume"] <= 0.0
        assert not any(e[0] == "fader" and e[1] in (1, 2) and e[2] > 0.0 for e in cl2.log)
        await ctl2.stop()

    run(body())


def test_profile_switched_in_settings_is_applied_at_connect(tmp_path: Path) -> None:
    """k1000.yaml now says dev, the playing engine still runs the m4 config (README: edit the
    pin, restart k1000): Main muted, new config uploaded, then the boot ramp."""
    async def body() -> None:
        m4 = load_profile("m4", {}, probe=False)
        st = State(master_db=-10.0)
        ctl, cl = make(tmp_path, st, playing=True, cfg=dc.generate(m4, st))
        await boot(ctl)
        log = nomark(cl.log)
        kinds = [e[0] for e in log]
        up = kinds.index("config")
        assert log[up][1]["title"] == "K1000 dev"
        assert log[:up] == [("mute", 0, True)]
        assert cl.log[0] == ("fader", MARKER_FADER, marker_db({0}))     # recorded before the mute
        after = log[up + 1:]
        unmute = after.index(("mute", 0, False))
        assert faders_sent(after[:unmute], 0) == [-40.0]               # parked low, then ramped
        assert cl.faders[0] == {"volume": -10.0, "mute": False}
        assert cl.cfg["devices"]["playback"]["device"] == "hw:sndrpihifiberry"
        await ctl.stop()

    run(body())


def test_engine_restart_while_running_is_ramped_in_again(tmp_path: Path) -> None:
    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-6.0))
        await boot(ctl)
        ctl.nudge("live_peq.0.gain", -4)
        assert await ctl.settle(3.0)
        ctl.save_now()
        cl.log.clear()
        active = yaml.safe_load((tmp_path / "active.yml").read_text())
        await cl.restart(active)                  # camilladsp killed, systemd restarts it muted
        await until(lambda: ctl.engine_ready and not ctl.ramping and cl.faders[0]["volume"] == -6.0,
                    what="re-ramp")
        assert faders_sent(cl.log, 0)[0] == -40.0
        assert cl.faders[0] == {"volume": -6.0, "mute": False}
        assert cl.cfg["filters"]["live_peq_1"]["parameters"]["gain"] == -2.0
        await ctl.stop()

    run(body())


def test_unmute_pressed_while_link_down_is_dropped_but_mute_is_kept(tmp_path: Path) -> None:
    async def body() -> None:
        # engine playing but muted by the operator; the link blips; unmute pressed meanwhile
        ctl, cl = make(tmp_path, State(master_db=-10.0, muted=True), playing=True)
        await boot(ctl)
        await cl.drop()
        assert ctl.status.engine is None
        ctl.set_mute(False)
        await asyncio.sleep(0.02)                 # pressed while the link is really down
        await cl.reconnect()
        await until(lambda: ctl.engine_ready, what="resync")
        assert await ctl.settle(2.0)
        assert ("mute", 0, False) not in cl.log
        assert ctl.state.muted is True and cl.faders[0]["mute"] is True
        assert ctl.focus.hint == ctl_mod.HINT_STILL_MUTED
        # a mute pressed while the link is down IS sent on reconnect
        ctl2, cl2 = make(tmp_path / "b", State(master_db=-10.0), playing=True)
        await boot(ctl2)
        await cl2.drop()
        ctl2.set_mute(True)
        await asyncio.sleep(0.02)
        await cl2.reconnect()
        await until(lambda: cl2.faders[0]["mute"] is True, what="pending mute sent")
        assert ctl2.state.muted is True
        await ctl.stop()
        await ctl2.stop()

    run(body())


def test_mute_then_unmute_while_link_down_leaves_nothing_pending(tmp_path: Path) -> None:
    """The last request (unmute) is already what the engine does: nothing to send, and Main must
    not stay excluded from adoption (a phone move after the reconnect is adopted)."""
    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-10.0), playing=True)
        await boot(ctl)
        await cl.drop()
        ctl.set_mute(True)
        ctl.set_mute(False)
        await asyncio.sleep(0.02)
        await cl.reconnect()
        await until(lambda: ctl.engine_ready, what="resync")
        assert await ctl.settle(2.0)
        assert not any(e[0] == "mute" and e[1] == 0 for e in cl.log)
        cl.faders[0]["volume"] = -14.0                      # the phone, after the reconnect
        ctl._own_fader.clear()
        await ctl.poll_once()
        assert ctl.state.master_db == -14.0 and ctl.state.muted is False
        await ctl.stop()

    run(body())


def test_local_edit_made_while_link_down_wins_on_reconnect(tmp_path: Path) -> None:
    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-10.0), playing=True)
        await boot(ctl)
        await cl.drop()
        ctl.nudge("live_peq.0.gain", -4)          # -2 dB while the engine is unreachable
        await cl.reconnect()
        await until(lambda: ctl.engine_ready, what="resync")
        assert await ctl.settle(3.0)
        assert ctl.state.live_peq[0].gain == -2.0
        assert cl.cfg["filters"]["live_peq_1"]["parameters"]["gain"] == -2.0
        await ctl.stop()

    run(body())


# ======================================================================= actions -> ops
def test_knob_actions_emit_the_expected_ops(tmp_path: Path) -> None:
    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-20.0), playing=True)
        await boot(ctl)

        async def act(fn: Any, *a: Any) -> list[tuple[Any, ...]]:
            n = len(cl.log)
            fn(*a)
            assert await ctl.settle(3.0)
            return cl.log[n:]

        assert await act(ctl.nudge, "master_db", 2) == [("fader", 0, -19.0)]
        # balance macro: Aux1 (subs) = +b/2, Aux2 (tops) = -b/2
        assert await act(ctl.nudge, "balance_db", 4) == [("fader", 1, 1.0), ("fader", 2, -1.0)]
        ops = await act(ctl.nudge, "live_peq.1.gain", -2)
        assert sets_for(ops, "/filters/live_peq_2/parameters/gain") == [-0.5, -1.0]
        ops = await act(ctl.nudge, "live_peq.1.freq", 1)
        assert sets_for(ops, "/filters/live_peq_2/parameters/freq")[-1] == pytest.approx(1000 * 2 ** (1 / 12), abs=0.01)
        # B3 Lo-Cut off: a frequency move to 10 Hz in click-safe steps, never a rebuild
        ops = await act(ctl.toggle, "lowcut_on")
        freqs = sets_for(ops, "/filters/top_lowcut/parameters/freq")
        assert freqs[-1] == 10.0 and len(freqs) > 10
        assert all(a > b for a, b in zip(freqs, freqs[1:]))
        assert not any(e[0] == "config" for e in ops)
        # sub delay: written under a subs-group mute (a delay change always clicks)
        ops = await act(ctl.nudge, "sub_delay_ms", 3)
        # the guard is recorded on the marker before the mute and cleared after the unmute
        assert ops == [("fader", MARKER_FADER, marker_db({1})), ("mute", 1, True),
                       ("set", "/filters/sub_delay/parameters/delay", dc.delay_on_grid(0.3)),
                       ("mute", 1, False), ("fader", MARKER_FADER, MARKER_DB)]
        # fine = one sample
        ops = await act(ctl.nudge, "sub_delay_ms", 1, True)
        assert sets_for(ops, "/filters/sub_delay/parameters/delay") == [dc.delay_on_grid(0.3 + 1 / 48)]
        # polarity: tops group guarded
        ops = await act(ctl.toggle, "top_inverted")
        assert nomark(ops) == [("mute", 2, True), ("set", "/filters/top_trim/parameters/inverted", True),
                               ("mute", 2, False)]
        # B4 reset live: smoothed back to flat
        ops = await act(ctl.reset_live)
        assert sets_for(ops, "/filters/live_peq_2/parameters/gain")[-1] == 0.0
        assert cl.cfg["filters"]["live_peq_2"]["parameters"]["freq"] == 1000.0   # default again
        await ctl.stop()

    run(body())


def test_every_value_is_clamped_to_the_contract_ranges(tmp_path: Path) -> None:
    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-2.0), playing=True)
        await boot(ctl)
        ctl.nudge("master_db", 1000)
        ctl.nudge("balance_db", -1000)
        ctl.nudge("live_peq.0.gain", 100)
        ctl.set_value("input_trim_db", 99)
        ctl.set_value("out_trim_db.3", -99)
        ctl.set_value("sub_delay_ms", float("nan"))
        ctl.set_value("live_peq.2.q", 1e9)
        assert await ctl.settle(5.0)
        s = ctl.state
        assert s.master_db == 0.0 and s.balance_db == -12.0
        assert s.live_peq[0].gain == RANGES["live_peq.gain"].max == 6.0
        assert s.input_trim_db == 12.0 and s.out_trim_db[3] == -24.0
        assert s.sub_delay_ms == 0.0 and s.live_peq[2].q == 16.0
        assert max(faders_sent(cl.log, 0)) <= 0.0
        assert max(faders_sent(cl.log, 1)) <= 6.0 and min(faders_sent(cl.log, 2)) >= -6.0
        assert cl.faders[1]["volume"] == -6.0 and cl.faders[2]["volume"] == 6.0
        assert cl.cfg["filters"]["in_trim"]["parameters"]["gain"] == 12.0
        # unknown fields never reach the engine and never raise into the UI
        n = len(cl.log)
        assert ctl.nudge("no_such_field", 1) is None
        assert ctl.set_value("live_peq.9.gain", 1) is None
        assert not ctl.toggle("master_db")
        assert len(cl.log) == n
        await ctl.stop()

    run(body())


def test_fast_turns_coalesce_while_a_plan_runs(tmp_path: Path) -> None:
    async def body() -> None:
        ctl, cl = make(tmp_path, State(), playing=True, sleep=asyncio.sleep)
        await boot(ctl)
        n = len(cl.log)
        for _ in range(12):                 # 12 detents in 60 ms (a fast E3 turn)
            ctl.nudge("sub_delay_ms", 1)
            await asyncio.sleep(0.005)
        assert await ctl.settle(5.0)
        ops = cl.log[n:]
        writes = sets_for(ops, "/filters/sub_delay/parameters/delay")
        assert writes[-1] == dc.delay_on_grid(1.2)
        assert len(writes) <= 2 and ops.count(("mute", 1, True)) <= 2   # one or two dips, not 12
        await ctl.stop()

    run(body())


def test_mute_is_sent_at_once_even_while_a_plan_runs(tmp_path: Path) -> None:
    async def body() -> None:
        gate = asyncio.Event()
        blocked = asyncio.Event()
        calls = 0

        async def gated(s: float) -> None:
            nonlocal calls
            calls += 1
            if calls == 3 and not gate.is_set():    # mid-way through a smoothed reset
                blocked.set()
                await gate.wait()
            await asyncio.sleep(0)

        st = State()
        for b in st.live_peq:
            b.gain = -12.0
        ctl, cl = make(tmp_path, st, playing=True)
        await boot(ctl)
        ctl._sleep = gated
        ctl.reset_live()
        await asyncio.wait_for(blocked.wait(), 2.0)
        n = len(cl.log)
        ctl.set_mute(True)
        await until(lambda: ("mute", 0, True) in cl.log[n:], what="immediate mute")
        assert ctl._busy                      # the plan is still in flight
        gate.set()
        assert await ctl.settle(5.0)
        assert cl.faders[0]["mute"] is True
        assert all(cl.cfg["filters"][f"live_peq_{i}"]["parameters"]["gain"] == 0.0 for i in range(1, 5))
        await ctl.stop()

    run(body())


def test_mute_pressed_during_the_boot_ramp_cancels_its_unmute(tmp_path: Path) -> None:
    async def body() -> None:
        gate = asyncio.Event()
        at_guard = asyncio.Event()

        async def gated(s: float) -> None:
            if s == dc.GUARD_WAIT_S and not gate.is_set():   # between the park and the unmute
                at_guard.set()
                await gate.wait()
            await asyncio.sleep(0)

        ctl, cl = make(tmp_path, State(master_db=-6.0), sleep=gated)
        await ctl.start()
        await asyncio.wait_for(at_guard.wait(), 2.0)
        ctl.set_mute(True)
        await until(lambda: ("mute", 0, True) in cl.log, what="operator mute")
        gate.set()
        await until(lambda: ctl.engine_ready and not ctl.ramping, what="ramp end")
        assert ("mute", 0, False) not in cl.log
        assert cl.faders[0]["mute"] is True and ctl.state.muted is True
        await ctl.stop()

    run(body())


def test_master_turned_down_during_the_ramp_caps_it(tmp_path: Path) -> None:
    async def body() -> None:
        steps = 0
        ctl: Controller

        async def counting(s: float) -> None:
            nonlocal steps
            steps += 1
            if steps == 4:
                ctl.nudge("master_db", -40)     # operator pulls the master down mid-ramp
            await asyncio.sleep(0)

        ctl, cl = make(tmp_path, State(master_db=0.0), sleep=counting)
        await boot(ctl)
        after = faders_sent(cl.log[cl.log.index(("mute", 0, False)):], 0)
        assert max(after) <= -10.0 + 1e-9 or all(v <= -10.0 for v in after[2:])
        assert cl.faders[0]["volume"] == -20.0 and ctl.state.master_db == -20.0
        await ctl.stop()

    run(body())


# ======================================================================= setup actions
def test_setup_actions_are_refused_unless_confirmed_or_muted(tmp_path: Path) -> None:
    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-10.0), playing=True)
        await boot(ctl)
        assert not ctl.toggle("mono_subs")
        assert ctl.state.mono_subs is False and ctl.focus.hint == ctl_mod.HINT_CONFIRM_SETUP
        assert await ctl.settle(2.0)
        assert not any(e[0] == "config" for e in cl.log)
        # confirmed (SETUP double push): Main muted around the upload, then back
        n = len(cl.log)
        assert ctl.toggle("mono_subs", confirmed=True)
        assert await ctl.settle(5.0)
        ops = nomark(cl.log[n:])
        assert [e[0] for e in ops] == ["mute", "config", "mute"]
        assert ops[0] == ("mute", 0, True) and ops[2] == ("mute", 0, False)
        assert cl.faders[MARKER_FADER]["volume"] == MARKER_DB              # guard record cleared
        split = ops[1][1]["mixers"]["split"]["mapping"]
        assert len(split[0]["sources"]) == 2 and ctl.state.mono_subs is True
        assert ops[1][1]["devices"] == dc.generate(DEV, State())["devices"]   # no device change
        # muted: allowed without confirm, and stays muted
        ctl.set_mute(True)
        assert await ctl.settle(2.0)
        n = len(cl.log)
        assert ctl.toggle("dev_audition")
        assert await ctl.settle(5.0)
        assert [e[0] for e in cl.log[n:]] == ["config"]
        assert ctl.state.dev_audition == "tops"
        assert cl.cfg["mixers"]["output"]["mapping"][0]["sources"][0]["channel"] == 2
        await ctl.stop()
        saved = json.loads((tmp_path / "state.json").read_text())
        assert saved["mono_subs"] is True and saved["dev_audition"] == "tops"

    run(body())


def test_profile_switch_refused_while_settings_pin_it(tmp_path: Path) -> None:
    async def body() -> None:
        ctl, cl = make(tmp_path, State(), playing=True,
                       profile_choice=ProfileChoice("dev", "settings"))
        await boot(ctl)
        assert not ctl.toggle("profile", confirmed=True)
        assert ctl.focus.hint == ctl_mod.HINT_PINNED and ctl.state.profile == "dev"
        await ctl.stop()

    run(body())


def test_failed_setup_is_reverted(tmp_path: Path) -> None:
    async def body() -> None:
        ctl, cl = make(tmp_path, State(), playing=True)
        await boot(ctl)

        async def refuse(text: str, timeout: float = 10.0) -> None:
            raise CdspError("SetConfig", "ConfigValidationError", "fake")

        cl.set_config_yaml = refuse  # type: ignore[method-assign]
        n = len(cl.log)
        ctl.toggle("mono_subs", confirmed=True)
        assert await ctl.settle(5.0)
        assert ctl.state.mono_subs is False
        assert nomark(cl.log[n:]) == [("mute", 0, True), ("mute", 0, False)]   # audio back as it was
        await ctl.stop()

    run(body())


def test_setup_interrupted_by_an_engine_exit_is_reverted(tmp_path: Path) -> None:
    """Switching to a profile whose device is absent: the engine exits during the upload
    (production unit, started from a file). The SETUP fields go back; nothing saved the new one."""
    async def body() -> None:
        m4 = load_profile("m4", {}, probe=False)
        ctl, cl = make(tmp_path, State(), playing=True, profile_loader=lambda name: m4)
        await boot(ctl)

        async def engine_exits(text: str, timeout: float = 10.0) -> None:
            await cl.drop()
            raise CdspUnavailable("engine exited on a failed device open")

        cl.set_config_yaml = engine_exits  # type: ignore[method-assign]
        assert ctl.toggle("profile", confirmed=True)
        await until(lambda: not ctl.engine_ready and not ctl._busy, what="link lost")
        assert ctl.state.profile == "dev" and ctl.profile.name == "dev"
        assert yaml.safe_load((tmp_path / "active.yml").read_text())["title"] == "K1000 dev"
        await ctl.stop()

    run(body())


# ======================================================================= adoption / drift
def test_external_edits_are_adopted_not_reverted_and_saved(tmp_path: Path) -> None:
    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-20.0), playing=True)
        await boot(ctl)
        n = len(cl.log)
        cl.cfg["filters"]["sys_peq_3"]["parameters"]["gain"] = -3.0        # laptop editor
        cl.cfg["filters"]["live_peq_2"]["parameters"]["freq"] = 3150.0
        cl.faders[0]["volume"] = -15.0                                     # phone Main
        cl.faders[1]["volume"], cl.faders[2]["volume"] = 2.0, -2.0         # GUI Aux pair
        await ctl.poll_once()
        s = ctl.state
        assert s.sys_peq[2].gain == -3.0 and s.live_peq[1].freq == 3150.0
        assert s.master_db == -15.0 and s.balance_db == 4.0
        assert cl.log[n:] == []                                            # nothing fought back
        await until(lambda: (tmp_path / "state.json").exists()
                    and json.loads((tmp_path / "state.json").read_text())["sys_peq"][2]["gain"] == -3.0,
                    what="adopted edit persisted")
        active = yaml.safe_load((tmp_path / "active.yml").read_text())
        assert active["filters"]["sys_peq_3"]["parameters"]["gain"] == -3.0
        await ctl.stop()

    run(body())


def test_phone_move_right_after_our_own_write_is_adopted_not_reverted(tmp_path: Path) -> None:
    """Regression (seen on the real engine): the phone moves Main within OWN_WRITE_S of our own
    Main write. The state must adopt it on a later poll; nothing may push the old level back."""
    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-13.0), playing=True)
        await boot(ctl)
        ctl.set_mute(True)
        await until(lambda: cl.faders[0]["mute"] is True, what="mute")
        ctl.set_mute(False)
        await until(lambda: cl.faders[0]["mute"] is False, what="unmute")
        n = len(cl.log)
        cl.faders[0]["volume"] = -20.0          # phone, inside the own-write window
        await ctl.poll_once()
        assert await ctl.settle(2.0)
        assert not any(e[0] == "fader" and e[1] == 0 for e in cl.log[n:]), cl.log[n:]
        ctl._own_fader.clear()                  # the window has passed
        await ctl.poll_once()
        assert await ctl.settle(2.0)
        assert ctl.state.master_db == -20.0 and cl.faders[0]["volume"] == -20.0
        assert not any(e[0] == "fader" and e[1] == 0 for e in cl.log[n:]), cl.log[n:]
        await ctl.stop()

    run(body())


def test_out_of_range_and_fixed_values_are_pushed_back(tmp_path: Path) -> None:
    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-20.0), playing=True)
        await boot(ctl)
        n = len(cl.log)
        cl.cfg["filters"]["live_peq_1"]["parameters"]["gain"] = 20.0       # beyond +6 dB
        cl.cfg["filters"]["top_lowcut"]["parameters"]["q"] = 3.0           # a fixed parameter
        cl.faders[0]["volume"] = 8.0                                       # Main above range
        cl.faders[1]["volume"], cl.faders[2]["volume"] = 3.0, 3.0          # common mode: not a balance
        cl.faders[1]["mute"] = True                                        # the GUI's Aux mute
        await ctl.poll_once()
        assert ctl.state.live_peq[0].gain == 6.0 and ctl.state.master_db == 0.0
        assert ctl.state.balance_db == 0.0
        assert cl.cfg["filters"]["live_peq_1"]["parameters"]["gain"] == 6.0
        assert cl.cfg["filters"]["top_lowcut"]["parameters"]["q"] == 0.707
        # pulled DOWN in click-safe steps, never one big write
        gains = sets_for(cl.log[n:], "/filters/live_peq_1/parameters/gain")
        assert len(gains) > 10 and all(abs(b - a) <= 0.5 + 1e-9 for a, b in zip([20.0] + gains, gains))
        qs = sets_for(cl.log[n:], "/filters/top_lowcut/parameters/q")
        assert len(qs) > 10 and all(abs(math.log2(b / a)) <= 1 / 12 + 1e-6 for a, b in zip([3.0] + qs, qs))
        assert cl.faders[0]["volume"] == 0.0
        # the raise (common mode +3) is pulled down; the mute is someone else's: kept, shown
        assert cl.faders[1] == {"volume": 0.0, "mute": True} and cl.faders[2]["volume"] == 0.0
        assert ("mute", 1, False) not in cl.log[n:]
        assert "SUBS GROUP MUTED" in ctl.status.drift and "SUBS GROUP MUTED" in ctl.status.message
        assert not any(e[0] == "config" for e in cl.log[n:])
        await ctl.stop()

    run(body())


def test_pipeline_change_is_restored_under_a_mute_after_a_second_look(tmp_path: Path) -> None:
    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-20.0), playing=True)
        await boot(ctl)
        cl.cfg["pipeline"] = cl.cfg["pipeline"][:-2] + cl.cfg["pipeline"][-1:]    # GUI dropped a step
        cl.cfg["filters"]["out_trim_2"]["parameters"]["mute"] = True     # and muted Top L
        cl.cfg["filters"]["top_trim"]["parameters"]["gain"] = -6.0       # and cut the tops
        n = len(cl.log)
        await ctl.poll_once()
        assert not any(e[0] == "config" for e in cl.log[n:])       # first look: wait
        await ctl.poll_once()
        ops = nomark(cl.log[n:])
        assert [e[0] for e in ops][:3] == ["mute", "config", "mute"]
        assert ops[0] == ("mute", 0, True) and ops[2] == ("mute", 0, False)
        assert cl.cfg["pipeline"] == dc.generate(DEV, ctl.state)["pipeline"]
        # the structure is put back, the cuts made outside K1000 are not undone
        assert cl.cfg["filters"]["out_trim_2"]["parameters"]["mute"] is True
        assert cl.cfg["filters"]["top_trim"]["parameters"]["gain"] == -6.0
        n = len(cl.log)
        await ctl.poll_once()                                       # next look: shown, kept
        assert {"out_trim_2 MUTED", "top_trim -6.0 dB"} <= set(ctl.status.drift)
        assert not any(e[0] in ("config", "set", "patch") for e in cl.log[n:])
        await ctl.stop()

    run(body())


def test_device_drift_is_shown_never_fixed_while_playing(tmp_path: Path) -> None:
    async def body() -> None:
        ctl, cl = make(tmp_path, State(), playing=True)
        await boot(ctl)
        cl.cfg["devices"]["volume_limit"] = 12.0
        n = len(cl.log)
        await ctl.poll_once()
        await ctl.poll_once()
        assert not any(e[0] in ("config", "patch", "set") for e in cl.log[n:])
        assert "DEVICES" in ctl.status.message and ctl.status.drift
        await ctl.stop()

    run(body())


def test_refused_edit_falls_back_to_the_engine_value(tmp_path: Path) -> None:
    async def body() -> None:
        ctl, cl = make(tmp_path, State(), playing=True)
        await boot(ctl)
        cl.refuse.add("/filters/live_peq_1/parameters/q")
        ctl.nudge("live_peq.0.q", 1)
        assert await ctl.settle(3.0)
        assert ctl.state.live_peq[0].q == 4.0
        assert ctl.focus.hint == "ENGINE REFUSED THE CHANGE"
        await ctl.stop()

    run(body())


# ======================================================================= persistence
def test_persistence_is_debounced_and_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    writes: list[str] = []
    real = ctl_mod.atomic_write

    def counting(path: Any, data: Any, mode: int = 0o644) -> None:
        writes.append(Path(path).name)
        real(path, data, mode)

    monkeypatch.setattr(ctl_mod, "atomic_write", counting)

    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-20.0), playing=True,
                       persist_debounce_s=0.3, persist_max_delay_s=5.0)
        await boot(ctl)
        await asyncio.sleep(0.4)
        writes.clear()
        for _ in range(10):                      # 10 detents in ~0.1 s
            ctl.nudge("live_peq.3.gain", -1)
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)
        assert writes == []                      # still turning / within the debounce
        await until(lambda: len(writes) >= 2, timeout=2.0, what="debounced save")
        await asyncio.sleep(0.4)
        assert sorted(writes) == ["active.yml", "state.json"]     # once each
        saved = json.loads((tmp_path / "state.json").read_text())
        assert State.from_dict(saved).to_dict() == ctl.persist_state().to_dict()
        assert saved["live_peq"][3]["gain"] == -5.0
        active = yaml.safe_load((tmp_path / "active.yml").read_text())
        assert active["title"] == "K1000 dev"
        assert active["filters"]["live_peq_4"]["parameters"]["gain"] == -5.0
        assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []   # no tmp left
        # nothing changed -> nothing written
        writes.clear()
        assert ctl.save_now() and writes == []
        await ctl.stop()

    run(body())


def test_start_writes_active_config_for_the_waiting_engine(tmp_path: Path) -> None:
    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-7.0))
        cl.connected = False

        async def never(timeout: float | None = None) -> bool:     # engine not up yet
            return False

        cl.connect = never  # type: ignore[method-assign]
        await ctl.start()
        active = yaml.safe_load((tmp_path / "active.yml").read_text())
        assert active["title"] == "K1000 dev"
        assert json.loads((tmp_path / "state.json").read_text())["master_db"] == -7.0
        await ctl.poll_once()
        assert ctl.status.engine is None                           # ENGINE DOWN on the screen
        ctl.nudge("master_db", 2)                                  # knobs still edit the state
        assert ctl.state.master_db == -6.0
        await ctl.stop()

    run(body())


def test_stop_persists_and_never_mutes(tmp_path: Path) -> None:
    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-20.0), playing=True)
        await boot(ctl)
        ctl.nudge("master_db", 4)
        n = len(cl.log)
        await ctl.stop()
        assert not any(e[0] == "mute" for e in cl.log[n:])
        assert cl.faders[0] == {"volume": -18.0, "mute": False}
        assert json.loads((tmp_path / "state.json").read_text())["master_db"] == -18.0
        assert cl.on_connect is None and cl.on_disconnect is None

    run(body())


# ======================================================================= UI plumbing
def test_knob_and_button_events_through_input_map(tmp_path: Path) -> None:
    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-20.0), playing=True)
        await boot(ctl)
        ctl.handle_event(EncoderEvent(0, 1, time.monotonic()))        # E1 = master
        assert ctl.state.master_db == -19.5
        ctl.handle_event(SwitchEvent(5, "short", time.monotonic()))   # B2 short: hint only
        assert ctl.focus.hint is not None and "B2" in ctl.focus.hint
        assert ctl.state.muted is False
        ctl.handle_event(SwitchEvent(5, "long", time.monotonic()))    # B2 long: mute
        assert ctl.state.muted is True
        await until(lambda: cl.faders[0]["mute"] is True, what="mute reaches the engine")
        ctl.handle_event(SwitchEvent(5, "long", time.monotonic()))    # B2 long again: unmute
        await until(lambda: cl.faders[0]["mute"] is False, what="unmute")
        ctl.handle_event(SwitchEvent(4, "press", time.monotonic()))   # B1 next page
        assert ctl.page == "LIVE"
        ctl.handle_event(EncoderEvent(0, 1, time.monotonic()))        # E1 on LIVE = slot select
        ctl.handle_event(EncoderEvent(2, -2, time.monotonic()))       # E3 = gain of PEQ 2
        assert ctl.state.live_peq[1].gain == -1.0
        ctl.handle_event(SwitchEvent(7, "long", time.monotonic()))    # B4 long: arms only
        assert ctl.state.live_peq[1].gain == -1.0
        assert ctl.focus.hint_now() == "HOLD B4 AGAIN TO RESET ALL LIVE EQ"
        ctl.handle_event(SwitchEvent(7, "long", time.monotonic()))    # B4 long again: reset
        assert ctl.state.live_peq[1].gain == 0.0
        assert ctl.focus.hint_now() == ctl_mod.HINT_LIVE_RESET        # said on the screen
        ctl.handle_event(SwitchEvent(6, "short", time.monotonic()))   # B3 on LIVE: hint only
        assert ctl.state.lowcut_on is True and "MAIN" in (ctl.focus.hint_now() or "")
        ctl.goto_page("MAIN")
        ctl.handle_event(SwitchEvent(6, "short", time.monotonic()))   # B3 on MAIN: Lo-Cut off
        assert ctl.state.lowcut_on is False
        assert ctl.focus.hint_now() == "LO-CUT OFF" and ctl.focus.editing_now() == "lowcut_on"
        ctl.handle_event(SwitchEvent(7, "long", time.monotonic()))    # B4 off LIVE: hint only
        assert "LIVE" in (ctl.focus.hint_now() or "")
        assert await ctl.settle(5.0)
        await ctl.stop()

    run(body())


def test_status_meters_and_clip_latch(tmp_path: Path) -> None:
    async def body() -> None:
        ctl, cl = make(tmp_path, State(), playing=True)
        await boot(ctl)
        cl.peaks = [-12.0, 0.2]
        await ctl.meter_once()
        st = ctl.status
        assert st.levels == [-12.0, 0.2, -12.0, 0.2]           # dev 'sum': each side on sub+top
        assert st.clip == [False, True, False, True]
        cl.peaks = [-30.0, -30.0]
        await ctl.meter_once()
        assert ctl.status.clip == [False, True, False, True]   # latched
        await ctl.poll_once()
        assert st.engine == "Running" and st.load == 1.5 and st.device_present is True
        await cl.drop()
        await until(lambda: ctl.status.engine is None, what="engine down shown")
        assert ctl.status.muted is None
        await ctl.stop()

    run(body())


def test_logical_levels_mapping() -> None:
    m4 = load_profile("m4", {}, probe=False)
    assert logical_levels(m4, "sum", [-1.0, -2.0, -3.0, -4.0]) == [-3.0, -4.0, -1.0, -2.0]
    assert logical_levels(DEV, "tops", [-5.0, -6.0]) == [-100.0, -100.0, -5.0, -6.0]
    assert logical_levels(DEV, "subs", [-5.0, -200.0]) == [-5.0, -100.0, -100.0, -100.0]
    assert logical_levels(DEV, "sum", None) == [-100.0] * 4
    assert logical_levels(DEV, "sum", [float("nan"), "x"]) == [-100.0] * 4


def test_settings_helpers_clamp_and_default() -> None:
    e = EngineSettings.from_settings({"engine": {"url": "ws://1.2.3.4:5", "boot_gain_db": 0,
                                                 "boot_ramp_s": "fast"}})
    assert e.url == "ws://1.2.3.4:5" and e.boot_gain_db == -20.0 and e.boot_ramp_s == 3.0
    assert EngineSettings.from_settings({"engine": "junk"}) == EngineSettings()
    assert EngineSettings.from_settings({}, url="ws://x:1").url == "ws://x:1"
    p = Paths.from_settings({"paths": {"state": "/tmp/s.json"}}, active_config="/tmp/a.yml")
    assert p == Paths(Path("/tmp/s.json"), Path("/tmp/a.yml"))
    assert Paths.from_settings({"_error": "x"}) == Paths()
    assert isinstance(Status().copy(), Status)


def test_console_commands(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from k1000.__main__ import run_command

    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-20.0), playing=True)
        await boot(ctl)
        stop = asyncio.Event()
        for line in ("nudge master_db 2", "set live_peq.0.gain -3", "toggle mono_subs",
                     "mute on", "page live", "status", "bogus", "nudge", "quit"):
            await run_command(ctl, line, stop)
        out = [l for l in capsys.readouterr().out.splitlines() if l.startswith("@")]
        assert out[0].startswith("@ok") and ctl.state.master_db == -19.0
        assert ctl.state.live_peq[0].gain == -3.0
        assert out[2].startswith("@err")                      # unconfirmed setup action
        assert ctl.state.muted is True and ctl.page == "LIVE"
        status = json.loads(out[5].split(" ", 1)[1])
        assert status["engine_ready"] is True and status["page"] == "LIVE"
        assert status["ready"] is True and status["alert"] == "" and status["in_clip"] is False
        assert out[6].startswith("@err") and out[7].startswith("@err") and stop.is_set()
        await ctl.stop()

    run(body())


def test_render_loop_draws_real_pages_at_a_capped_rate(tmp_path: Path) -> None:
    """__main__.render_loop with a fake framebuffer: real pages.render frames from the
    controller's Status (every field the screen reads), never above RENDER_MAX_FPS."""
    pytest.importorskip("PIL")
    from k1000 import __main__ as main_mod
    from k1000.ui import pages

    class FakeFb:
        def __init__(self) -> None:
            self.frames: list[tuple[float, Any]] = []

        def show(self, img: Any) -> bool:
            self.frames.append((time.monotonic(), img))
            return True

    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-20.0), playing=True, meter_s=0.02)
        await boot(ctl)
        fb = FakeFb()
        task = asyncio.get_running_loop().create_task(main_mod.render_loop(ctl, fb))
        try:
            await until(lambda: len(fb.frames) >= 3, timeout=10.0, what="first frames")
            n0, t0 = len(fb.frames), time.monotonic()
            for i in range(20):                 # meters + edits change ~50 times a second
                cl.peaks = [-20.0 + (i % 5), -18.0]
                ctl.nudge("master_db", 1 if i % 2 else -1)
                await asyncio.sleep(0.02)
            rate = (len(fb.frames) - n0) / (time.monotonic() - t0)
            assert rate <= main_mod.RENDER_MAX_FPS + 3, rate
            ctl.set_mute(True)
            await until(lambda: ctl.status.muted is True, what="mute")
            await asyncio.sleep(0.3)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await ctl.stop()
        img = fb.frames[-1][1]
        assert img.size == pages.SIZE
        assert pages.overlays_for(ctl.state, ctl.status) == ["MUTED"]
        assert not pages._warned_shapes or all(k[0] != "Status" for k in pages._warned_shapes)

    run(body())


def test_input_loop_feeds_knob_events_and_survives_bad_ones(tmp_path: Path) -> None:
    from k1000 import __main__ as main_mod
    from k1000.hw.mcp23017 import ControlsClosed

    class FakeControls:
        def __init__(self, events: list[Any]) -> None:
            self.events = list(events)

        async def next_event(self, timeout: float | None = None) -> Any:
            await asyncio.sleep(0)
            if not self.events:
                raise ControlsClosed("done")
            return self.events.pop(0)

    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-20.0), playing=True)
        await boot(ctl)
        now = time.monotonic()
        evs = [EncoderEvent(0, 2, now), object(), SwitchEvent(5, "long", now + 0.01)]
        await asyncio.wait_for(main_mod.input_loop(ctl, FakeControls(evs)), 2.0)
        assert ctl.state.master_db == -19.0 and ctl.state.muted is True
        await ctl.stop()

    run(body())


# ======================================================================= review fixes
def test_boot_ramp_holds_main_muted_while_the_engine_is_not_processing(tmp_path: Path) -> None:
    """A fader change made while the engine is Stalled is applied as ONE step when audio resumes
    (CamillaDSP README, Volume control): a ramp run then is no ramp. Main stays muted at the
    start level until the engine runs, then the ramp starts."""
    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-6.0))
        cl.engine_state = "Stalled"                 # capture delivers no data yet
        await ctl.start()
        await until(lambda: ctl.status.message == ctl_mod.MSG_HELD_MUTED, what="held muted")
        await asyncio.sleep(0.05)
        assert ("mute", 0, False) not in cl.log
        assert cl.faders[0] == {"volume": -40.0, "mute": True}
        assert ctl.ramping and not ctl.engine_ready and ctl.status.engine == "Stalled"
        assert decode_marker(cl.faders[MARKER_FADER]["volume"]) == {0}     # Main's ramp recorded
        assert not await ctl.settle(0.05)
        n = len(cl.log)
        cl.engine_state = "Running"
        await until(lambda: ctl.engine_ready and not ctl.ramping, what="ramp once running")
        after = nomark(cl.log[n:])
        assert after[0] == ("mute", 0, False)
        steps = [-40.0] + faders_sent(after, 0)
        deltas = [b - a for a, b in zip(steps, steps[1:])]
        assert steps[-1] == -6.0 and all(0 < d <= 6.0 for d in deltas) and len(deltas) >= 5
        assert cl.faders[MARKER_FADER]["volume"] == MARKER_DB and ctl.status.message == ""
        await ctl.stop()

    run(body())


def test_engine_stalling_mid_ramp_remutes_parks_and_restarts_the_ramp(tmp_path: Path) -> None:
    async def body() -> None:
        steps = 0
        cl: FakeClient

        async def sleeper(s: float) -> None:
            nonlocal steps
            if s == dc.VOLUME_RAMP_MS / 1000.0:      # one boot-ramp step
                steps += 1
                if steps == 4:
                    cl.engine_state = "Stalled"
            await asyncio.sleep(0)

        ctl, cl = make(tmp_path, State(master_db=-4.0), sleep=sleeper)
        await ctl.start()
        await until(lambda: ctl.status.message == ctl_mod.MSG_HELD_MUTED, what="held after the stall")
        log = nomark(cl.log)
        unmute = log.index(("mute", 0, False))
        assert len(faders_sent(log[unmute:], 0)) == 3 + 1      # 3 steps, then the re-park
        assert log[-2:] == [("mute", 0, True), ("fader", 0, -40.0)]   # nothing sent while Stalled
        assert cl.faders[0] == {"volume": -40.0, "mute": True}
        n = len(cl.log)
        cl.engine_state = "Running"
        await until(lambda: ctl.engine_ready and not ctl.ramping, what="ramp restarted")
        after = nomark(cl.log[n:])
        assert after[0] == ("mute", 0, False)
        ramp = [-40.0] + faders_sent(after, 0)
        assert ramp[-1] == -4.0 and all(0 < b - a <= 6.0 for a, b in zip(ramp, ramp[1:]))
        assert cl.faders[0] == {"volume": -4.0, "mute": False}
        await ctl.stop()

    run(body())


def test_setup_guard_unmute_waits_until_the_engine_runs(tmp_path: Path) -> None:
    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-10.0), playing=True)
        await boot(ctl)
        cl.engine_state = "Stalled"
        n = len(cl.log)
        assert ctl.toggle("mono_subs", confirmed=True)
        await until(lambda: ctl.status.message == ctl_mod.MSG_HELD_MUTED, what="guard holds Main")
        assert [e[0] for e in nomark(cl.log[n:])] == ["mute", "config"]
        assert cl.faders[0]["mute"] is True
        assert decode_marker(cl.faders[MARKER_FADER]["volume"]) == {0}
        cl.engine_state = "Running"
        assert await ctl.settle(3.0)
        assert nomark(cl.log[n:])[-1] == ("mute", 0, False)
        assert cl.faders[MARKER_FADER]["volume"] == MARKER_DB and ctl.state.mono_subs is True
        await ctl.stop()

    run(body())


def test_gui_group_mute_and_cut_are_kept_a_raise_is_pulled_down(tmp_path: Path) -> None:
    """CamillaGUI's Aux panel moves ONE fader (never a balance pair). K1000 must never unmute a
    group or raise it back on its own (the tune: measuring tops alone), only pull a raise down,
    and a cut must survive later balance moves and delay guards."""
    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-20.0), playing=True)
        await boot(ctl)
        n = len(cl.log)
        cl.faders[1]["mute"] = True                 # phone: subs group muted
        cl.faders[2]["volume"] = -10.0              # and the tops group 10 dB down
        await ctl.poll_once()
        await ctl.poll_once()
        assert cl.log[n:] == []
        assert cl.faders[1]["mute"] is True and cl.faders[2]["volume"] == -10.0
        assert {"SUBS GROUP MUTED", "TOPS GROUP -10.0 dB"} <= set(ctl.status.drift)
        assert "OUTSIDE K1000" in ctl.status.message
        # the operator turns the balance: the macro moves, the tops cut is carried along
        ctl.nudge("balance_db", 4)                  # b = 2: Aux1 +1, Aux2 -1 (-10 under it)
        assert await ctl.settle(3.0)
        assert cl.faders[1] == {"volume": 1.0, "mute": True}
        assert cl.faders[2] == {"volume": -11.0, "mute": False}
        ctl._own_fader.clear()
        await ctl.poll_once()
        assert cl.faders[2]["volume"] == -11.0 and ctl.state.balance_db == 2.0
        # a sub delay edit while the subs group is muted outside K1000: no guard on it
        n = len(cl.log)
        ctl.nudge("sub_delay_ms", 2)
        assert await ctl.settle(3.0)
        assert not any(e[0] == "mute" for e in cl.log[n:]) and cl.faders[1]["mute"] is True
        assert sets_for(cl.log[n:], "/filters/sub_delay/parameters/delay") == [dc.delay_on_grid(0.2)]
        # a one-sided RAISE is pulled down to the macro (the safe direction)
        cl.faders[2]["volume"] = 4.0
        ctl._own_fader.clear()
        await ctl.poll_once()
        assert cl.faders[2]["volume"] == -1.0
        # the phone unmutes the subs: nothing left to show
        cl.faders[1]["mute"] = False
        await ctl.poll_once()
        assert ctl.status.drift == [] and ctl.status.message == ""
        await ctl.stop()

    run(body())


def test_group_left_muted_by_a_killed_k1000_is_released_someone_elses_is_not(tmp_path: Path) -> None:
    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-20.0), playing=True)
        cl.faders[1]["mute"] = True
        cl.faders[MARKER_FADER]["volume"] = marker_db({1})   # killed inside a subs delay guard
        await boot(ctl)
        assert cl.faders[1]["mute"] is False and cl.faders[MARKER_FADER]["volume"] == MARKER_DB
        assert cl.faders[0] == {"volume": -20.0, "mute": False}   # Main untouched
        ctl2, cl2 = make(tmp_path / "b", State(master_db=-20.0), playing=True)
        cl2.faders[2]["mute"] = True                            # the phone's mute: no record
        await boot(ctl2)
        assert cl2.faders[2]["mute"] is True and ("mute", 2, False) not in cl2.log
        assert "TOPS GROUP MUTED" in ctl2.status.drift
        await ctl.stop()
        await ctl2.stop()

    run(body())


def test_gui_trim_cut_or_output_mute_is_kept_a_trim_raise_is_stepped_down(tmp_path: Path) -> None:
    """README §6: the laptop GUI mutes one output / trims the subs to measure a group. K1000's
    fixed values (Gain mute False, sub_trim 0 dB) must not be pushed back over that (an unmute
    or a +10 dB step), and a value above K1000's comes down in steps, not one write."""
    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-20.0), playing=True)
        await boot(ctl)
        for _ in range(3):                          # also across clean polls
            n = len(cl.log)
            cl.cfg["filters"]["out_trim_0"]["parameters"]["mute"] = True
            cl.cfg["filters"]["sub_trim"]["parameters"]["gain"] = -10.0
            await ctl.poll_once()
            assert cl.log[n:] == []
            assert cl.cfg["filters"]["out_trim_0"]["parameters"]["mute"] is True
            assert cl.cfg["filters"]["sub_trim"]["parameters"]["gain"] == -10.0
        assert {"out_trim_0 MUTED", "sub_trim -10.0 dB"} <= set(ctl.status.drift)
        cl.cfg["filters"]["out_trim_0"]["parameters"]["mute"] = False
        cl.cfg["filters"]["sub_trim"]["parameters"]["gain"] = 5.0
        n = len(cl.log)
        await ctl.poll_once()
        gains = sets_for(cl.log[n:], "/filters/sub_trim/parameters/gain")
        assert gains[-1] == 0.0 and len(gains) >= 50
        assert all(abs(b - a) <= 0.1 + 1e-9 for a, b in zip([5.0] + gains, gains))
        assert ctl.status.drift == [] and cl.cfg["filters"]["sub_trim"]["parameters"]["gain"] == 0.0
        await ctl.stop()

    run(body())


def test_muted_engine_without_marker_away_from_the_boot_gain_stays_muted(tmp_path: Path) -> None:
    """The engine restarted while k1000 was down, the phone unmuted it, played, then muted it
    between sets. Main muted at -15 is not a fresh engine (that reads -40): adopted, kept muted."""
    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-10.0))
        cl.faders[0] = {"volume": -15.0, "mute": True}
        await boot(ctl)
        assert ("mute", 0, False) not in cl.log and not any(e[:2] == ("fader", 0) for e in cl.log)
        assert cl.faders[0] == {"volume": -15.0, "mute": True}
        assert ctl.state.muted is True and ctl.state.master_db == -15.0
        assert cl.faders[MARKER_FADER]["volume"] == MARKER_DB     # brought in from now on
        await ctl.stop()

    run(body())


def test_stop_waits_for_a_poll_restore_and_never_leaves_main_muted(tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """SIGTERM while the 1 Hz poll is inside a pipeline restore (Main muted around the upload):
    stop() lets it finish; if it cannot (engine not processing) Main's guard stays recorded and
    the next k1000 ramps Main in, instead of adopting the guard's mute as the operator's."""
    monkeypatch.setattr(ctl_mod, "STOP_LOCK_S", 0.3)

    async def body() -> None:
        gate = asyncio.Event()
        at_guard = asyncio.Event()

        async def gated(s: float) -> None:
            if s == dc.GUARD_WAIT_S and not gate.is_set():
                at_guard.set()
                await gate.wait()
            await asyncio.sleep(0)

        # (a) the restore completes while stop() waits
        ctl, cl = make(tmp_path, State(master_db=-20.0), playing=True, poll_s=0.05, sleep=gated)
        await boot(ctl)
        cl.cfg["pipeline"] = cl.cfg["pipeline"][:-2] + cl.cfg["pipeline"][-1:]
        await asyncio.wait_for(at_guard.wait(), 5.0)
        assert cl.faders[0]["mute"] is True and not await ctl.settle(0.01)
        asyncio.get_running_loop().call_later(0.1, gate.set)
        await ctl.stop()
        assert cl.faders[0] == {"volume": -20.0, "mute": False}
        assert cl.faders[MARKER_FADER]["volume"] == MARKER_DB and ctl.state.muted is False

        # (b) it cannot finish (engine stalled): recorded; the next k1000 ramps Main in
        gate.clear()
        at_guard.clear()
        ctl, cl = make(tmp_path / "b", State(master_db=-20.0), playing=True, poll_s=0.05, sleep=gated)
        await boot(ctl)
        cl.cfg["pipeline"] = cl.cfg["pipeline"][:-2] + cl.cfg["pipeline"][-1:]
        await asyncio.wait_for(at_guard.wait(), 5.0)
        cl.engine_state = "Stalled"
        await ctl.stop(settle_timeout=0.1)
        assert cl.faders[0]["mute"] is True
        assert decode_marker(cl.faders[MARKER_FADER]["volume"]) == {0}
        assert json.loads((tmp_path / "b" / "state.json").read_text())["muted"] is False
        cl.on_connect = cl.on_disconnect = None
        cl.connected = False
        ctl2 = Controller(cl, DEV, ctl.state.copy(), ctl.paths, sleep=fast_sleep, poll_s=3600,
                          meter_s=3600, device_probe=lambda d: True)
        await ctl2.start()
        await until(lambda: ctl2.status.message == ctl_mod.MSG_HELD_MUTED, what="resume held")
        assert cl.faders[0] == {"volume": -40.0, "mute": True}     # parked for the ramp
        n = len(cl.log)
        cl.engine_state = "Running"
        await until(lambda: ctl2.engine_ready and not ctl2.ramping, what="resumed ramp")
        ramp = faders_sent(cl.log[n:], 0)
        assert ramp[-1] == -20.0 and all(0 < b - a <= 6.0 for a, b in zip([-40.0] + ramp, ramp))
        assert cl.faders[0] == {"volume": -20.0, "mute": False} and ctl2.state.muted is False
        assert cl.faders[MARKER_FADER]["volume"] == MARKER_DB
        await ctl2.stop()

    run(body())


def test_k1000_killed_mid_ramp_ramps_in_again_instead_of_adopting_the_halfway_level(tmp_path: Path) -> None:
    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-8.0), playing=True)
        cl.faders[0] = {"volume": -25.0, "mute": False}           # half-way up a ramp
        cl.faders[MARKER_FADER]["volume"] = marker_db({0})        # ...when that k1000 was killed
        await boot(ctl)
        assert ctl.state.master_db == -8.0                         # not lowered to -25
        assert cl.faders[0] == {"volume": -8.0, "mute": False}
        assert cl.faders[MARKER_FADER]["volume"] == MARKER_DB
        sent = faders_sent(cl.log, 0)
        assert min(sent) >= -25.0                                  # continued, no dip to -40
        assert all(0 <= b - a <= 6.0 for a, b in zip([-25.0] + sent, sent))
        await ctl.stop()

    run(body())


def test_failed_debounced_save_is_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = ctl_mod.atomic_write
    fail = {"on": False}
    calls: list[str] = []

    def flaky(path: Any, data: Any, mode: int = 0o644) -> None:
        calls.append(Path(path).name)
        if fail["on"]:
            raise OSError(28, "No space left on device")
        real(path, data, mode)

    monkeypatch.setattr(ctl_mod, "atomic_write", flaky)
    monkeypatch.setattr(ctl_mod, "SAVE_RETRY_S", (0.1, 0.2))

    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-20.0), playing=True,
                       persist_debounce_s=0.05, persist_max_delay_s=0.2)
        await boot(ctl)
        await asyncio.sleep(0.3)
        fail["on"] = True
        ctl.nudge("live_peq.0.gain", -6)            # a -3 dB notch
        await until(lambda: ctl.status.message == "CANNOT SAVE STATE", what="failed save")
        fail["on"] = False                          # the card recovers
        await until(lambda: json.loads((tmp_path / "state.json").read_text())["live_peq"][0]["gain"] == -3.0,
                    timeout=3.0, what="retried save")
        assert ctl.status.message == ""
        await ctl.stop()

    run(body())


def test_failed_save_at_start_is_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = ctl_mod.atomic_write
    fail = {"on": True}

    def flaky(path: Any, data: Any, mode: int = 0o644) -> None:
        if fail["on"]:
            raise OSError(5, "Input/output error")
        real(path, data, mode)

    monkeypatch.setattr(ctl_mod, "atomic_write", flaky)
    monkeypatch.setattr(ctl_mod, "SAVE_RETRY_S", (0.1,))

    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-7.0), playing=True)
        await boot(ctl)
        assert not (tmp_path / "state.json").exists() and ctl.status.message == "CANNOT SAVE STATE"
        fail["on"] = False
        await until(lambda: (tmp_path / "state.json").exists() and (tmp_path / "active.yml").exists(),
                    timeout=3.0, what="retried without any edit")
        assert json.loads((tmp_path / "state.json").read_text())["master_db"] == -7.0
        await until(lambda: ctl.status.message == "", what="message cleared")
        await ctl.stop()

    run(body())


def test_torn_state_json_is_rebuilt_from_active_yml_not_defaults(tmp_path: Path) -> None:
    tuned = State(master_db=-10.0, top_inverted=True, sub_delay_ms=1.5)
    tuned.sys_peq[2].gain = -4.0                    # the Friday mic tune
    active_text = dc.to_yaml(dc.generate(DEV, tuned))
    (tmp_path / "active.yml").write_text(active_text)
    (tmp_path / "state.json").write_text('{"schema": 1, "master_db": -10.0, "sys_peq": [{"fr')
    paths = Paths(tmp_path / "state.json", tmp_path / "active.yml")
    st, note = recover_state(paths)
    assert note == ctl_mod.NOTE_REBUILT and st.sys_peq[2].gain == -4.0 and st.top_inverted is True
    assert delay_samples(st.sub_delay_ms) == 72 and (tmp_path / "state.json.bad").exists()
    # the calibrated master is lost with state.json: start MUTED, never ramp into the default
    assert st.muted is True and st.master_db == State().master_db

    async def body() -> None:
        ctl, cl = make(tmp_path, st, cfg=yaml.safe_load(active_text))     # the engine booted it
        await boot(ctl)
        on_disk = yaml.safe_load((tmp_path / "active.yml").read_text())
        assert on_disk["filters"]["sys_peq_3"]["parameters"]["gain"] == -4.0
        assert cl.cfg["filters"]["sys_peq_3"]["parameters"]["gain"] == -4.0
        assert json.loads((tmp_path / "state.json").read_text())["sys_peq"][2]["gain"] == -4.0
        # fresh engine: parked at the (default) master, still muted; the operator decides
        assert ("mute", 0, False) not in cl.log
        assert cl.faders[0] == {"volume": State().master_db, "mute": True}
        await ctl.stop()

    run(body())
    # first boot (nothing on disk): defaults, unmuted, nothing to say
    st2, note2 = recover_state(Paths(tmp_path / "x" / "s.json", tmp_path / "x" / "a.yml"))
    assert note2 == "" and st2.to_dict() == State().to_dict()
    # state.json moved away (README "Sound state") while active.yml shows a set-up box:
    # defaults, but MUTED (the show master is unknown)
    st4, note4 = recover_state(Paths(tmp_path / "x" / "s.json", tmp_path / "active.yml"))
    assert note4 == ctl_mod.NOTE_MISSING and st4.muted is True
    assert st4.to_dict() == State(muted=True).to_dict()
    # a good state.json wins over active.yml
    good = Paths(tmp_path / "g" / "s.json", tmp_path / "active.yml")
    good.state.parent.mkdir()
    good.state.write_text(json.dumps(State(master_db=-3.0).to_dict()))
    st3, note3 = recover_state(good)
    assert note3 == "" and st3.master_db == -3.0 and st3.sys_peq[2].gain == 0.0


def test_rebuilt_muted_state_adopts_a_playing_engine_unchanged(tmp_path: Path) -> None:
    """k1000 restarted after a torn state.json while the engine plays at the show level: the
    rebuilt state starts muted, but a playing engine is adopted as it plays (no mute, no ramp,
    no level change): audio never depends on the UI."""
    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-35.0), playing=True)
        ctl.state.muted = True                          # what recover_state hands over
        ctl._applied.muted = True
        await boot(ctl)
        assert not any(e[0] in ("mute", "fader") and e[1] == 0 for e in cl.log), cl.log
        assert cl.faders[0] == {"volume": -35.0, "mute": False}
        assert ctl.state.muted is False and ctl.state.master_db == -35.0
        await ctl.stop()

    run(body())


def test_first_overwrite_of_active_yml_keeps_the_previous_one(tmp_path: Path) -> None:
    async def body() -> None:
        old = dc.to_yaml(dc.generate(DEV, State(master_db=-5.0, sub_delay_ms=2.0)))
        (tmp_path / "active.yml").write_text(old)
        ctl, cl = make(tmp_path, State(master_db=-5.0))
        await boot(ctl)
        assert (tmp_path / "active.yml.prev").read_text() == old
        assert (tmp_path / "active.yml").read_text() != old
        await ctl.stop()

    run(body())


def test_boot_ramp_steps_stay_small_whatever_the_settings(tmp_path: Path) -> None:
    async def body() -> None:
        for i, (ramp, gain) in enumerate(((0.1, -40.0), (0.5, -80.0), (3.0, -80.0))):
            ctl, cl = make(tmp_path / str(i), State(master_db=0.0), boot_ramp_s=ramp, boot_gain_db=gain)
            await boot(ctl)
            sent = faders_sent(cl.log, 0)
            deltas = [b - a for a, b in zip(sent, sent[1:])]
            assert sent[0] == gain and sent[-1] == 0.0
            assert max(deltas) <= ctl_mod.BOOT_STEP_MAX_DB + 1e-9, (ramp, gain, sent)
            await ctl.stop()

    run(body())


def test_nudge_refuses_non_finite_or_absurd_detents(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from k1000.__main__ import run_command

    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-80.0), playing=True)
        await boot(ctl)
        for d in (math.inf, -math.inf, math.nan, 1e6, "x"):
            assert ctl.nudge("master_db", d) is None          # type: ignore[arg-type]
            assert ctl.nudge("lowcut_hz", d) is None          # type: ignore[arg-type]
        assert ctl.state.master_db == -80.0 and ctl.state.lowcut_hz == 100.0
        assert ctl.nudge("lowcut_hz", 1000) == 300.0           # big but sane: clamped, no overflow
        stop = asyncio.Event()
        await run_command(ctl, "nudge master_db inf", stop)
        out = [l for l in capsys.readouterr().out.splitlines() if l.startswith("@")]
        assert out[-1].startswith("@err") and ctl.state.master_db == -80.0
        await ctl.stop()

    run(body())


def test_marker_encoding_round_trips() -> None:
    assert marker_db() == MARKER_DB and decode_marker(MARKER_DB) == frozenset()
    for g in ({0}, {1}, {2}, {0, 1}, {1, 2}, {0, 1, 2}):
        assert decode_marker(marker_db(g)) == g
        assert decode_marker(marker_db(g) + 2e-6) == g                # f32 read-back
    for v in (None, 0.0, -40.0, -61.7, -62.0, -60.2, float("nan"), True):
        assert decode_marker(v) is None


# ======================================================================= engine tests
def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


class Engine:
    """A private camilladsp started like the production unit (-m -g-40: boots muted) from the
    controller's own active.yml, on ENGINE_PORT."""

    def __init__(self, cfg_path: Path, logfile: Path, port: int = ENGINE_PORT) -> None:
        self.cfg_path, self.logfile, self.port = cfg_path, logfile, port
        self.proc: subprocess.Popen[bytes] | None = None

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    def start(self) -> None:
        cfg = yaml.safe_load(self.cfg_path.read_text())
        for side in ("capture", "playback"):        # never the ALSA card from a test
            assert cfg["devices"][side]["type"] in ("SignalGenerator", "File", "RawFile"), cfg["devices"][side]
        deadline = time.monotonic() + 5.0
        while _port_open(self.port):
            if time.monotonic() > deadline:
                raise RuntimeError(f"port {self.port} busy: a stale camilladsp?")
            time.sleep(0.05)
        with open(self.logfile, "ab") as log:
            self.proc = subprocess.Popen([CDSP_BIN, "-p", str(self.port), "-a", "127.0.0.1", "-m",
                                          "-g-40", str(self.cfg_path)], stdout=log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 5.0
        while not _port_open(self.port):
            if self.proc.poll() is not None:
                raise RuntimeError("camilladsp exited:\n" + self.logfile.read_text()[-2000:])
            if time.monotonic() > deadline:
                raise RuntimeError("camilladsp websocket did not open in 5 s")
            time.sleep(0.02)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def kill(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(5)
        self.proc = None


def _test_transform(cfg: dict[str, Any]) -> dict[str, Any]:
    return dc.signal_generator_variant(cfg, level_db=-20.0)


class FaderSampler:
    """Records the engine's Main fader every ~15 ms from a second client (an observer, like
    CamillaGUI would be)."""

    def __init__(self, url: str) -> None:
        self.client = CamillaClient(url, backoff_min=0.05, backoff_max=0.2)
        self.samples: list[tuple[float, float, bool]] = []
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await self.client.connect(timeout=3.0)
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        while True:
            try:
                f = await self.client.get_faders()
                self.samples.append((time.monotonic(), f[0]["volume"], f[0]["mute"]))
            except (CdspUnavailable, CdspError):
                pass
            await asyncio.sleep(0.015)

    def mark(self) -> int:
        return len(self.samples)

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
        await self.client.close()


def assert_ramp(samples: list[tuple[float, float, bool]], master: float) -> None:
    """Muted at -40 first; unmuted no higher than one step above it; then only up, by small
    steps, to the master."""
    assert samples, "no fader samples"
    first_unmuted = next(i for i, s in enumerate(samples) if not s[2])
    assert any(s[2] and s[1] == -40.0 for s in samples[:first_unmuted]), samples[:first_unmuted][:5]
    vols = [s[1] for s in samples[first_unmuted:]]
    assert vols[0] <= -40.0 + 6.0, vols[:3]
    distinct = [vols[0]] + [b for a, b in zip(vols, vols[1:]) if b != a]
    deltas = [b - a for a, b in zip(distinct, distinct[1:])]
    assert all(0 < d <= 6.0 for d in deltas), deltas
    assert distinct[-1] == pytest.approx(master, abs=1e-6)
    assert not any(s[2] for s in samples[first_unmuted:]), "re-muted during the ramp"


needs_engine = pytest.mark.skipif(not os.access(CDSP_BIN, os.X_OK), reason="no camilladsp binary")


@pytest.mark.engine
@needs_engine
def test_engine_controller_follows_a_scripted_show(tmp_path: Path) -> None:
    async def body() -> None:
        paths = Paths(tmp_path / "state.json", tmp_path / "active.yml")
        st = State(master_db=-12.0)
        client = CamillaClient(f"ws://127.0.0.1:{ENGINE_PORT}", backoff_min=0.05, backoff_max=0.3,
                               fader_max_db=ctl_mod.FADER_CEILING_DB)
        ctl = Controller(client, DEV, st, paths, transform=_test_transform, device_probe=None,
                         poll_s=0.2, meter_s=0.1, persist_debounce_s=0.2, boot_ramp_s=3.0)
        ctl.save_now()                                   # the engine unit waits for this file
        eng = Engine(paths.active_config, tmp_path / "camilladsp.log")
        await asyncio.to_thread(eng.start)
        sampler = FaderSampler(eng.url)
        obs = CamillaClient(eng.url, backoff_min=0.05, backoff_max=0.2)
        try:
            await sampler.start()
            await obs.connect(timeout=3.0)
            # ---- boot: muted engine -> verify -> -40 -> unmute -> ramp to -12
            await ctl.start()
            await until(lambda: ctl.engine_ready and not ctl.ramping, timeout=8.0, what="boot ramp")
            await asyncio.sleep(0.1)
            assert_ramp(sampler.samples, -12.0)
            faders = await obs.get_faders()
            assert faders[0] == {"volume": -12.0, "mute": False}
            assert faders[MARKER_FADER]["volume"] == pytest.approx(MARKER_DB, abs=1e-3)
            assert (await obs.get_config_json())["title"] == "K1000 dev"

            async def val(ptr: str) -> Any:
                return await obs.get_value(ptr)

            # ---- scripted actions; the engine follows
            ctl.nudge("master_db", -2)
            ctl.nudge("balance_db", 4)
            ctl.nudge("live_peq.1.gain", -6)
            ctl.nudge("live_peq.1.freq", 12)
            assert await ctl.settle(5.0)
            f = await obs.get_faders()
            assert (f[0]["volume"], f[1]["volume"], f[2]["volume"]) == (-13.0, 1.0, -1.0)
            assert await val("/filters/live_peq_2/parameters/gain") == -3.0
            assert await val("/filters/live_peq_2/parameters/freq") == pytest.approx(2000.0)
            ctl.toggle("lowcut_on")
            ctl.nudge("sub_delay_ms", 10)
            ctl.toggle("sub_inverted")
            assert await ctl.settle(5.0)
            assert await val("/filters/top_lowcut/parameters/freq") == 10.0
            assert await val("/filters/sub_delay/parameters/delay") == pytest.approx(dc.delay_on_grid(1.0))
            assert await val("/filters/sub_trim/parameters/inverted") is True
            assert (await obs.get_faders())[1] == {"volume": 1.0, "mute": False}   # guard released
            ctl.reset_live()
            assert await ctl.settle(5.0)
            assert await val("/filters/live_peq_2/parameters/gain") == 0.0
            # audio flows when unmuted, stops when muted
            await asyncio.sleep(0.3)
            assert max(await obs.command("GetPlaybackSignalPeak")) > -60.0
            ctl.set_mute(True)
            await until(lambda: ctl.status.muted is True, what="mute")
            assert (await obs.get_faders())[0]["mute"] is True
            await asyncio.sleep(0.6)
            assert max(await obs.command("GetPlaybackSignalPeak")) < -150.0
            ctl.set_mute(False)
            await until(lambda: ctl.status.muted is False, what="unmute")

            # ---- the phone/laptop edits; the controller adopts (never fights) and saves
            await obs.set_value("/filters/live_peq_3/parameters/gain", -4.5)
            await obs.set_fader(0, -20.0)
            await until(lambda: ctl.state.live_peq[2].gain == -4.5 and ctl.state.master_db == -20.0,
                        timeout=3.0, what="adoption")
            await asyncio.sleep(0.5)
            assert await val("/filters/live_peq_3/parameters/gain") == -4.5
            assert (await obs.get_faders())[0]["volume"] == -20.0
            await until(lambda: json.loads(paths.state.read_text())["live_peq"][2]["gain"] == -4.5,
                        timeout=3.0, what="saved")

            # ---- engine killed + restarted by systemd: comes back muted, controller ramps in
            await asyncio.to_thread(eng.kill)
            await until(lambda: ctl.status.engine is None, timeout=3.0, what="ENGINE DOWN")
            mark = sampler.mark()
            await asyncio.to_thread(eng.start)
            await until(lambda: ctl.engine_ready and not ctl.ramping and ctl.status.muted is False,
                        timeout=10.0, what="re-ramp after the restart")
            await asyncio.sleep(0.1)
            assert_ramp(sampler.samples[mark:], -20.0)
            assert await val("/filters/live_peq_3/parameters/gain") == -4.5
            assert await val("/filters/top_lowcut/parameters/freq") == 10.0

            # ---- controller stopped: the engine keeps playing, unchanged
            before = (await obs.get_faders(), await obs.get_config_json())
            await ctl.stop()
            await client.close()
            await asyncio.sleep(1.0)
            assert eng.alive()
            assert await obs.get_state() == "Running"
            assert (await obs.get_faders(), await obs.get_config_json()) == before
        finally:
            await ctl.stop()
            await client.close()
            await sampler.close()
            await obs.close()
            await asyncio.to_thread(eng.kill)

    run(body(), limit=90.0)


@pytest.mark.engine
@needs_engine
def test_engine_boot_ramp_waits_for_audio_to_flow(tmp_path: Path) -> None:
    """Measured on the OUTPUT samples (File playback), not on GetFaders: the engine starts on a
    capture that has no data yet (a FIFO with a writer that sends nothing: Stalled, like a USB
    interface starting up). K1000 must hold Main muted at -40 for as long as that lasts and ramp
    in once the program flows. Before the fix, the first audible block came out at the full
    master (-20 dBFS sine at master -12 -> -32 dBFS at once). Then the capture pauses mid-ramp
    (Stalled again): Main is muted and parked, and audio comes back from silence with a new
    ramp, never at the level the ramp had reached meanwhile."""
    np = pytest.importorskip("numpy")
    fifo, out = tmp_path / "in.fifo", tmp_path / "out.raw"
    os.mkfifo(fifo)
    master, level_db, sr = -12.0, -20.0, 48000

    def transform(cfg: dict[str, Any]) -> dict[str, Any]:
        cfg = copy.deepcopy(cfg)
        cfg["devices"]["capture"] = {"type": "RawFile", "channels": 2, "filename": str(fifo),
                                     "format": "S32_LE"}
        cfg["devices"]["playback"] = {"type": "File", "channels": 2, "filename": str(out),
                                      "format": "S32_LE"}
        return cfg

    def feed(wfd: int, seconds: float, k0: int = 0) -> int:
        """1 kHz at level_db on both channels, paced in real time like a sound card."""
        amp = 10 ** (level_db / 20) * (2 ** 31 - 1)
        n, k, t0 = 256, 0, time.monotonic()
        while k * n < seconds * sr:
            idx = np.arange((k0 + k) * n, (k0 + k + 1) * n)
            sig = (amp * np.sin(2 * math.pi * 1000 * idx / sr)).astype(np.int32)
            os.write(wfd, np.repeat(sig, 2).tobytes())
            k += 1
            dt = t0 + k * n / sr - time.monotonic()
            if dt > 0:
                time.sleep(dt)
        return k0 + k

    async def body() -> None:
        paths = Paths(tmp_path / "state.json", tmp_path / "active.yml")
        client = CamillaClient(f"ws://127.0.0.1:{ENGINE_PORT}", backoff_min=0.05, backoff_max=0.3,
                               fader_max_db=ctl_mod.FADER_CEILING_DB)
        ctl = Controller(client, DEV, State(master_db=master), paths, transform=transform,
                         device_probe=None, poll_s=0.5, meter_s=0.2, boot_ramp_s=3.0)
        ctl.save_now()
        wfd = os.open(fifo, os.O_RDWR)          # a writer exists, but no data yet
        eng = Engine(paths.active_config, tmp_path / "camilladsp.log")
        await asyncio.to_thread(eng.start)
        obs = CamillaClient(eng.url, backoff_min=0.05, backoff_max=0.2)
        try:
            await obs.connect(timeout=3.0)
            await ctl.start()
            await asyncio.sleep(4.5)               # longer than the whole 3 s ramp
            assert await obs.get_state() != "Running"
            f = await obs.get_faders()
            assert f[0] == {"volume": -40.0, "mute": True}, f[0]
            assert ctl.ramping and ctl.status.message == ctl_mod.MSG_HELD_MUTED
            k = await asyncio.to_thread(feed, wfd, 1.3)          # the program starts
            assert ctl.ramping                                   # ...and the ramp with it
            await asyncio.sleep(1.5)                             # the capture pauses mid-ramp
            assert (await obs.get_faders())[0] == {"volume": -40.0, "mute": True}
            assert ctl.status.message == ctl_mod.MSG_HELD_MUTED
            await asyncio.to_thread(feed, wfd, 5.0, k)           # and comes back
            assert ctl.engine_ready and not ctl.ramping
            assert (await obs.get_faders())[0] == {"volume": master, "mute": False}
        finally:
            os.close(wfd)
            await ctl.stop()
            await client.close()
            await obs.close()
            await asyncio.to_thread(eng.kill)

        left = np.frombuffer(out.read_bytes(), dtype=np.int32)[0::2].astype(np.float64) / 2 ** 31
        per_s = sr / 256
        db = [20 * math.log10(max(float(np.max(np.abs(left[i:i + 256]))), 1e-12))
              for i in range(0, len(left) - 255, 256)]
        audible = [v > -150 for v in db]
        first = audible.index(True)
        gap = audible.index(False, first)                  # silence: the re-park after the pause
        again = audible.index(True, gap)
        last = len(audible) - 1 - audible[::-1].index(True)
        target = level_db + master
        reach = next(i for i in range(again, len(db)) if db[i] >= target - 1.0)
        tail = sorted(db[last - int(0.5 * per_s):last])
        hundred = int(0.1 * per_s)
        print(f"first audible: {[round(v, 1) for v in db[first:first + 6]]} dBFS; before the "
              f"pause up to {max(db[first:gap]):.1f}; after it: "
              f"{[round(v, 1) for v in db[again:again + 6]]}; reaches {target - 1:.0f} dBFS "
              f"{(reach - again) / per_s:.2f} s later; tail median {tail[len(tail) // 2]:.1f} dBFS")
        assert max(db[first:first + hundred]) <= level_db - 40.0 + 3.0     # starts at the boot gain
        assert max(db[first:gap]) < target - 6.0                           # still ramping at the pause
        assert gap - first > hundred and again - gap > 5
        assert max(db[again:again + hundred]) <= level_db - 40.0 + 3.0    # back from the boot gain
        assert (reach - again) / per_s >= 2.0                              # a ramp of seconds
        assert abs(tail[len(tail) // 2] - target) <= 1.0                   # ends at the master

    run(body(), limit=60.0)


class K1000Proc:
    """`python3 -m k1000 --headless --no-hw --stdin --test-devices` as a subprocess."""

    def __init__(self, settings: Path, logfile: Path) -> None:
        self.settings, self.logfile = settings, logfile
        self.proc: subprocess.Popen[str] | None = None
        self.lines: list[str] = []
        self._lock = threading.Lock()

    def start(self) -> None:
        env = dict(os.environ, PYTHONPATH=str(BOX), PYTHONUNBUFFERED="1", PYTHONDONTWRITEBYTECODE="1")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "k1000", "--settings", str(self.settings), "--headless",
             "--no-hw", "--no-phone", "--stdin", "--test-devices"], cwd=str(BOX), env=env, text=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        with open(self.logfile, "a") as log:
            for line in self.proc.stdout:
                log.write(line)
                with self._lock:
                    self.lines.append(line.rstrip("\n"))

    def send(self, cmd: str) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        self.proc.stdin.write(cmd + "\n")
        self.proc.stdin.flush()

    def replies(self, kind: str) -> list[str]:
        with self._lock:
            return [l for l in self.lines if l.startswith("@" + kind)]

    def status(self, timeout: float = 3.0) -> dict[str, Any]:
        n = len(self.replies("status"))
        self.send("status")
        deadline = time.monotonic() + timeout
        while len(self.replies("status")) <= n:
            if time.monotonic() > deadline:
                raise AssertionError("no @status reply:\n" + "\n".join(self.lines[-20:]))
            time.sleep(0.02)
        return json.loads(self.replies("status")[-1].split(" ", 1)[1])

    def wait_status(self, pred: Any, timeout: float, what: str) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            st = self.status()
            if pred(st):
                return st
            if time.monotonic() > deadline:
                raise AssertionError(f"{what}: last status {st}\n" + "\n".join(self.lines[-30:]))
            time.sleep(0.1)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def kill(self, sig: int = signal.SIGKILL) -> int | None:
        if self.proc is None:
            return None
        if self.proc.poll() is None:
            self.proc.send_signal(sig)
        try:
            return self.proc.wait(8)
        finally:
            self.proc = None


@pytest.mark.engine
@needs_engine
def test_engine_service_process_kill_restart_and_sigterm(tmp_path: Path) -> None:
    state_p, active_p = tmp_path / "state.json", tmp_path / "active.yml"
    settings = tmp_path / "k1000.yaml"
    settings.write_text(yaml.safe_dump({
        "profile": "dev",
        "profiles": {"dev": {"probe": False}},
        "engine": {"url": f"ws://127.0.0.1:{ENGINE_PORT}", "boot_gain_db": -40.0, "boot_ramp_s": 3.0},
        "paths": {"state": str(state_p), "active_config": str(active_p)},
    }))
    state_p.write_text(json.dumps(State(master_db=-14.0).to_dict()))
    eng = Engine(active_p, tmp_path / "camilladsp.log")
    k = K1000Proc(settings, tmp_path / "k1000.log")

    async def snapshot(c: CamillaClient) -> tuple[Any, Any]:
        return await c.get_faders(), await c.get_config_json()

    def snap() -> tuple[Any, Any]:
        async def go() -> tuple[Any, Any]:
            async with CamillaClient(eng.url) as c:
                return await snapshot(c)
        return asyncio.run(go())

    try:
        k.start()
        deadline = time.monotonic() + 10
        while not active_p.exists():                   # k1000 writes it, the engine waits for it
            assert time.monotonic() < deadline, "\n".join(k.lines)
            time.sleep(0.05)
        eng.start()
        st = k.wait_status(lambda s: s["engine_ready"] and not s["ramping"] and s["muted"] is False,
                           15.0, "service boot ramp")
        assert st["engine"] in ("Running", "Paused")
        f, _cfg = snap()
        assert f[0] == {"volume": -14.0, "mute": False}

        k.send("nudge master_db 4")
        k.send("nudge live_peq.0.gain -4")
        k.send("settle 5")
        k.wait_status(lambda s: True, 5.0, "after settle")
        deadline = time.monotonic() + 5
        while snap()[0][0]["volume"] != -12.0:
            assert time.monotonic() < deadline, "master nudge not applied"
            time.sleep(0.05)
        k.send("mute on")
        deadline = time.monotonic() + 3
        while not snap()[0][0]["mute"]:
            assert time.monotonic() < deadline, "mute not applied"
            time.sleep(0.05)
        k.send("mute off")
        deadline = time.monotonic() + 3
        while snap()[0][0]["mute"]:
            assert time.monotonic() < deadline, "unmute not applied"
            time.sleep(0.05)
        time.sleep(0.3)

        # ---- SIGKILL the service: the engine keeps playing exactly as it was
        before = snap()
        k.kill(signal.SIGKILL)
        time.sleep(1.0)
        assert eng.alive()
        assert snap() == before
        assert before[1]["filters"]["live_peq_1"]["parameters"]["gain"] == -2.0

        # ---- restart the service: adopts, never mutes or dips the playing engine
        samples: list[tuple[float, bool]] = []
        stop_sampling = threading.Event()

        def sample() -> None:
            async def go() -> None:
                async with CamillaClient(eng.url) as c:
                    while not stop_sampling.is_set():
                        f = await c.get_faders()
                        samples.append((f[0]["volume"], f[0]["mute"]))
                        await asyncio.sleep(0.01)
            asyncio.run(go())

        th = threading.Thread(target=sample, daemon=True)
        th.start()
        k.start()
        st = k.wait_status(lambda s: s["engine_ready"], 10.0, "service re-attach")
        time.sleep(1.5)                                 # a few polls
        stop_sampling.set()
        th.join(3)
        assert samples and all(s == (-12.0, False) for s in samples), sorted(set(samples))
        state_view = k.wait_status(lambda s: True, 3.0, "status")
        assert state_view["muted"] is False and not state_view["ramping"]
        assert snap() == before

        # ---- SIGTERM: clean exit, state saved, audio left as it is
        code = k.kill(signal.SIGTERM)
        assert code == 0
        assert eng.alive() and snap() == before
        saved = json.loads(state_p.read_text())
        assert saved["master_db"] == -12.0 and saved["live_peq"][0]["gain"] == -2.0
        assert saved["muted"] is False
    finally:
        k.kill(signal.SIGKILL)
        eng.kill()


# ======================================================================= integration-review fixes
def _band(ctl: Controller) -> str:
    """What the MUTED band would say right now (pages._mute_hint over a controller snapshot)."""
    from k1000.ui import pages
    page, st, status, focus = ctl.snapshot()
    return pages._mute_hint(pages._Ctx(focus, time.monotonic()), st, status)[1]


def test_b2_during_a_held_boot_ramp_keeps_main_muted(tmp_path: Path) -> None:
    """Engine not processing yet, K1000 holds Main muted before its ramp: the screen says Main
    comes in by itself and B2 keeps it muted; B2 long then MUTES (the ramp is cancelled into a
    held mute), it never sends an unmute into an engine that is not running."""
    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-10.0))
        cl.engine_state = "Starting"
        await ctl.start()
        await until(lambda: ctl.status.message == ctl_mod.MSG_HELD_MUTED, what="held muted")
        assert ctl.displayed_muted() and ctl.state.muted is False
        assert _band(ctl).startswith("RAMPS IN BY ITSELF")
        assert ctl.b2_muted() is False
        n = len(cl.log)
        ctl.handle_event(SwitchEvent(5, "long", time.monotonic()))
        assert ctl.state.muted is True
        await asyncio.sleep(0.05)
        assert ("mute", 0, False) not in cl.log[n:]
        assert _band(ctl) == ctl_mod.MSG_HELD_MUTED          # the reason, not READY
        cl.engine_state = "Running"
        await until(lambda: ctl.engine_ready and not ctl.ramping, what="ramp finished")
        assert ("mute", 0, False) not in cl.log
        assert cl.faders[0]["mute"] is True and ctl.displayed_muted()
        assert _band(ctl) == "READY · hold B2 to unmute"
        await ctl.stop()

    run(body())


def test_operator_unmute_during_a_setup_guard_waits_for_the_engine(tmp_path: Path) -> None:
    """A setup guard holds Main muted AT THE SHOW LEVEL while the engine is not processing: an
    unmute sent then would come in as a step on resume. The operator's unmute is recorded and
    goes out only once the guard brought Main back with the engine running."""
    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-10.0), playing=True)
        await boot(ctl)

        async def pause_on_upload(entry: tuple[Any, ...]) -> None:
            if entry[0] == "config":
                cl.engine_state = "Paused"
        cl.on_write = pause_on_upload
        assert ctl.toggle("mono_subs", confirmed=True)
        await until(lambda: ctl.status.message == ctl_mod.MSG_HELD_MUTED, what="guard holds Main")
        assert _band(ctl) == ctl_mod.MSG_HELD_MUTED and ctl.b2_muted() is False
        n = len(cl.log)
        ctl.set_mute(True)                  # e.g. the console, then changing one's mind
        ctl.set_mute(False)
        await asyncio.sleep(0.1)
        assert ("mute", 0, False) not in cl.log[n:] and cl.faders[0]["mute"] is True
        m = len(cl.log)
        cl.engine_state = "Running"
        assert await ctl.settle(3.0)
        assert ("mute", 0, False) in cl.log[m:] and cl.faders[0] == {"volume": -10.0, "mute": False}
        assert ctl.state.muted is False and ctl.state.mono_subs is True
        await ctl.stop()

    run(body())


def test_stop_during_a_setup_guard_never_releases_a_held_unmute(tmp_path: Path) -> None:
    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-10.0), playing=True)
        await boot(ctl)

        async def pause_on_upload(entry: tuple[Any, ...]) -> None:
            if entry[0] == "config":
                cl.engine_state = "Paused"
        cl.on_write = pause_on_upload
        assert ctl.toggle("mono_subs", confirmed=True)
        await until(lambda: ctl.status.message == ctl_mod.MSG_HELD_MUTED, what="guard holds Main")
        n = len(cl.log)
        ctl.set_mute(True)
        ctl.set_mute(False)
        await ctl.stop()
        assert ("mute", 0, False) not in cl.log[n:] and cl.faders[0]["mute"] is True
        assert FADER_MAIN_BIT in decode_marker(cl.faders[MARKER_FADER]["volume"])   # next k1000

    run(body())


FADER_MAIN_BIT = dc.FADER_MAIN


def test_knobs_offline_is_an_alert_on_the_screen(tmp_path: Path) -> None:
    ctl, cl = make(tmp_path, State())
    assert ctl.status.knobs_ok is None and ctl.status.message == ""
    ctl.set_knobs_ok(False)
    assert ctl.status.message == ctl_mod.MSG_KNOBS_OFFLINE == ctl.status.alert
    ctl.set_knobs_ok(True)
    assert ctl.status.message == "" and ctl.status.alert == ""


def test_knobs_offline_names_the_phone_mute_only_when_there_is_one(tmp_path: Path) -> None:
    """SAFE-1: after a no-go (no phone servers, AP down) or with SETUP › Phone off, there is no
    phone mute: the alert says what is left. It follows the phone while the knobs stay offline."""
    class Phone:
        remote_mute_ready = True

    ctl, cl = make(tmp_path, State())
    ph = Phone()
    ctl.phone = ph
    ctl.phone_changed()                                    # knobs fine: nothing to say
    assert ctl.status.message == ""
    ctl.set_knobs_ok(False)
    assert ctl.status.alert == ctl_mod.MSG_KNOBS_OFFLINE_PHONE == "KNOBS OFFLINE · USE PHONE MUTE / BYPASS"
    ph.remote_mute_ready = False                           # SETUP › Phone off, or the AP went down
    ctl.phone_changed()
    assert ctl.status.alert == ctl_mod.MSG_KNOBS_OFFLINE and "PHONE" not in ctl.status.alert
    ph.remote_mute_ready = True
    ctl.phone_changed()
    assert ctl.status.alert == ctl_mod.MSG_KNOBS_OFFLINE_PHONE
    ctl.phone = None                                       # --no-phone, or the surface failed
    ctl.phone_changed()
    assert ctl.status.alert == ctl_mod.MSG_KNOBS_OFFLINE
    ctl.phone = object()                                   # a surface without the property: no phone
    ctl.phone_changed()
    assert ctl.status.alert == ctl_mod.MSG_KNOBS_OFFLINE
    ctl.set_knobs_ok(True)
    ctl.phone = ph
    ctl.phone_changed()
    assert ctl.status.alert == "" and ctl.status.message == ""


def test_low_priority_notes_are_not_alerts(tmp_path: Path) -> None:
    """Drift notes (a GUI cut kept) must not replace the unmute line in the MUTED band."""
    ctl, cl = make(tmp_path, State())
    ctl._set_message("held", 1, "TOPS GROUP -3.0 dB OUTSIDE K1000")
    assert ctl.status.message and ctl.status.alert == ""
    ctl._set_message("setup", 4, "APPLYING SETUP…")
    assert ctl.status.alert == "APPLYING SETUP…"


def test_edits_toward_silence_are_saved_at_once(tmp_path: Path) -> None:
    """A power cut right after B2 (or after pulling the master down) must not boot the PA back
    unmuted / louder: those edits skip the debounce. Raising the level keeps it."""
    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-10.0), playing=True,
                       persist_debounce_s=30.0, persist_max_delay_s=60.0)
        await boot(ctl)
        sp = tmp_path / "state.json"

        def saved() -> dict[str, Any]:
            return json.loads(sp.read_text())

        ctl.set_mute(True)
        await until(lambda: saved()["muted"] is True, timeout=1.0, what="mute saved at once")
        ctl.nudge("master_db", -10)
        await until(lambda: saved()["master_db"] == -15.0, timeout=1.0, what="master down saved")
        ctl.nudge("master_db", 4)                       # up: debounced as before
        await asyncio.sleep(0.6)
        assert saved()["master_db"] == -15.0
        await ctl.stop()                                # ...and saved on the way out
        assert saved()["master_db"] == -13.0

    run(body())


def test_invisible_meter_changes_do_not_wake_the_screen(tmp_path: Path) -> None:
    """Input noise hovering around the meter floor re-rendered identical frames at 10 fps."""
    async def body() -> None:
        ctl, cl = make(tmp_path, State(), playing=True)
        await boot(ctl)
        cl.peaks = [-99.7, -99.7]
        await ctl.meter_once()
        v = ctl.version
        for p in ([-100.0, -100.0], [-70.0, -88.0], [-99.2, -61.0]):
            cl.peaks = p
            await ctl.meter_once()
        assert ctl.version == v and ctl.status.levels[0] == -99.2      # stored, not redrawn
        cl.peaks = [-10.0, -10.0]
        await ctl.meter_once()
        assert ctl.version > v
        v = ctl.version
        cl.peaks = [-10.05, -9.95]                       # under one meter step
        await ctl.meter_once()
        assert ctl.version == v
        cl.peaks = [-12.0, -10.0]
        await ctl.meter_once()
        assert ctl.version > v
        await ctl.stop()

    run(body())


def test_input_clip_is_latched_from_the_program_inputs(tmp_path: Path) -> None:
    async def body() -> None:
        ctl, cl = make(tmp_path, State(), playing=True)
        await boot(ctl)
        await ctl.meter_once()
        assert ctl.status.in_clip is False                 # engine without the command: off
        cl.cap_peaks = [-20.0, -0.3]                       # dev input_map (0, 1): R hits the ADC
        await ctl.meter_once()
        assert ctl.status.in_clip is True and ctl.status.clip == [False] * 4
        cl.cap_peaks = [-20.0, -20.0]
        await ctl.meter_once()
        assert ctl.status.in_clip is True                  # latched CLIP_HOLD_S
        await ctl.stop()

    run(body())


def gui_default_config() -> dict[str, Any]:
    """CamillaGUI's built-in config (what a page loaded while the engine was down holds)."""
    return {"title": None, "description": None,
            "devices": {"samplerate": 48000, "chunksize": 1024, "capture_samplerate": 44100,
                        "resampler": {"type": "AsyncSinc", "profile": "Balanced"},
                        "volume_limit": None, "enable_rate_adjust": False,
                        "capture": {"type": "Alsa", "channels": 2, "device": "hw:0"},
                        "playback": {"type": "Alsa", "channels": 2, "device": "hw:0"}},
            "mixers": {}, "filters": {}, "processors": {}, "pipeline": []}


def test_engine_moved_to_another_card_is_put_back(tmp_path: Path) -> None:
    """A CamillaGUI Apply of its default config moves the engine to hw:0 (on m4: the
    HiFiBerry, the PA goes silent). With the profile's card present, K1000 uploads the
    profile's whole config (device restart, Main muted, then the boot ramp), after a second
    look, at most once per RESTORE_MIN_INTERVAL_S. A pipeline restore that would keep hw:0 is
    not attempted."""
    async def body() -> None:
        st = State(master_db=-12.0)
        st.sys_peq[1].gain = -3.0
        ctl, cl = make(tmp_path, st, playing=True)
        ctl.same_card = lambda a, b: a == b            # hw:0 is NOT this profile's card here
        await boot(ctl)
        expected_devices = copy.deepcopy(cl.cfg["devices"])
        cl.cfg = gui_default_config()                  # the phone pressed Apply
        n = len(cl.log)
        await ctl.poll_once()                          # first look: nothing yet
        assert not any(e[0] == "config" for e in cl.log[n:])
        await ctl.poll_once()                          # second look: repair
        assert await ctl.settle(5.0)
        ups = [e[1] for e in cl.log[n:] if e[0] == "config"]
        assert len(ups) == 1 and ups[0]["devices"] == expected_devices
        assert ups[0]["filters"]["sys_peq_2"]["parameters"]["gain"] == -3.0   # the tune
        after = nomark(cl.log[n:])
        assert after.index(("mute", 0, True)) < after.index(("config", ups[0]))
        ramp = faders_sent(after[after.index(("config", ups[0])):], 0)
        assert ramp[0] == -40.0 and ramp[-1] == -12.0                     # boot ramp, no step
        assert cl.faders[0] == {"volume": -12.0, "mute": False}
        # the same thing again right away: rate-limited
        cl.cfg = gui_default_config()
        m = len(cl.log)
        await ctl.poll_once()
        await ctl.poll_once()
        assert not any(e[0] == "config" for e in cl.log[m:])
        assert "PIPELINE CHANGED" in ctl.status.message or "DEVICES" in ctl.status.message
        await ctl.stop()

    run(body())


def test_device_options_alone_never_restart_the_engine(tmp_path: Path) -> None:
    """Same card under another name (hw:0 = hw:sndrpihifiberry), or another chunksize: not a
    wrong card. Reported, never repaired while audio runs."""
    async def body() -> None:
        ctl, cl = make(tmp_path, State(), playing=True)
        ctl.same_card = lambda a, b: True
        await boot(ctl)
        cl.cfg["devices"]["capture"]["device"] = "hw:0"
        cl.cfg["devices"]["chunksize"] = 1024
        n = len(cl.log)
        for _ in range(3):
            await ctl.poll_once()
        assert not any(e[0] in ("config", "mute") for e in cl.log[n:])
        assert ctl.status.drift and "DEVICES" in ctl.status.message
        await ctl.stop()

    run(body())


def test_no_device_repair_while_the_profile_card_is_absent(tmp_path: Path) -> None:
    """Moving the engine to an absent card would only trade one silence for an engine restart
    loop: the repair waits for the card (NO DEVICE says why meanwhile)."""
    async def body() -> None:
        present = {"v": True}
        ctl, cl = make(tmp_path, State(), playing=True, device_probe=lambda d: present["v"])
        ctl.same_card = lambda a, b: a == b
        await boot(ctl)
        present["v"] = False
        cl.cfg = gui_default_config()
        n = len(cl.log)
        for _ in range(3):
            await ctl.poll_once()
        assert await ctl.settle(5.0)
        ups = [e[1]["devices"]["playback"]["device"] for e in cl.log[n:] if e[0] == "config"]
        assert "hw:sndrpihifiberry" not in ups      # (a pipeline restore may keep hw:0)
        present["v"] = True                     # the card is back: repaired after a 2nd look
        await ctl.poll_once()
        await ctl.poll_once()
        assert await ctl.settle(5.0)
        ups = [e[1]["devices"]["playback"]["device"] for e in cl.log[n:] if e[0] == "config"]
        assert ups[-1] == "hw:sndrpihifiberry"
        await ctl.stop()

    run(body())


def test_same_card_resolves_proc_links(tmp_path: Path) -> None:
    proc = tmp_path / "asound"
    (proc / "card0").mkdir(parents=True)
    (proc / "card1").mkdir()
    (proc / "sndrpihifiberry").symlink_to("card0")
    (proc / "M4").symlink_to("card1")
    same = ctl_mod._same_card
    assert same("hw:0", "hw:sndrpihifiberry", proc) is True
    assert same("hw:CARD=sndrpihifiberry,DEV=0", "hw:sndrpihifiberry", proc) is True
    assert same("hw:0", "hw:M4", proc) is False
    assert same("hw:7", "hw:M4", proc) is False          # the live card is gone
    assert same("hw:0", "hw:Nope", proc) is None         # profile's card unknown: cannot tell
    assert same("default", "hw:M4", proc) is None


def test_profile_routing_fixups_are_shown(tmp_path: Path) -> None:
    m4 = load_profile("m4", {"profiles": {"m4": {"input_map": [3, 4]}}}, probe=False)
    assert m4.input_map == (2, 3)                         # the profile's line inputs, not 0-1
    ctl = Controller(FakeClient(None), m4, State(), Paths(tmp_path / "s.json", tmp_path / "a.yml"))
    assert ctl.status.message == ctl_mod.MSG_PROFILE_ROUTING
    ok = Controller(FakeClient(None), load_profile("m4", {}, probe=False), State(),
                    Paths(tmp_path / "s.json", tmp_path / "a.yml"))
    assert ok.status.message == ""


def test_reset_live_says_so(tmp_path: Path) -> None:
    ctl, cl = make(tmp_path, State())
    ctl.reset_live()
    assert ctl.focus.hint_now() == ctl_mod.HINT_LIVE_FLAT
    ctl.state.live_peq[0].gain = -6.0
    ctl.reset_live()
    assert ctl.focus.hint_now() == ctl_mod.HINT_LIVE_RESET and ctl.state.live_peq[0].gain == 0.0


# ----------------------------------------------------------------------- service wiring
class FakeControls:
    """ZynthianControls surface for knobs_task: healthy flag, closable, events on demand."""

    def __init__(self) -> None:
        self.healthy = True
        self.closed = False
        self.queue: asyncio.Queue[Any] = asyncio.Queue()

    async def next_event(self, timeout: float | None = None) -> Any:
        from k1000.hw.mcp23017 import ControlsClosed
        ev = await self.queue.get()
        if ev is None:
            raise ControlsClosed("closed")
        return ev

    def close(self) -> None:
        self.closed = True


def test_knobs_are_retried_until_they_answer_and_offline_is_shown(tmp_path: Path) -> None:
    from k1000 import __main__ as main_mod

    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-20.0), playing=True)
        await boot(ctl)
        fc = FakeControls()
        calls: list[bool] = []

        def opener(settings: Any, quiet: bool = False) -> Any:
            calls.append(quiet)
            return fc if len(calls) >= 3 else None

        hw: dict[str, Any] = {"controls": None}
        seen: list[str] = []
        task = asyncio.get_running_loop().create_task(
            main_mod.knobs_task(ctl, {}, hw, retry_s=0.02, opener=opener))
        await until(lambda: (seen.append(ctl.status.message) or hw["controls"] is fc), what="opened")
        assert ctl_mod.MSG_KNOBS_OFFLINE in seen and calls[:3] == [False, True, True]
        await until(lambda: ctl.status.knobs_ok is True, what="knobs ok")
        assert ctl.status.message == ""
        fc.queue.put_nowait(SwitchEvent(5, "long", time.monotonic()))      # B2 works
        await until(lambda: ctl.state.muted is True, what="mute from the knobs")
        fc.queue.put_nowait(None)                       # the event stream fails: reopened
        await until(lambda: fc.closed and len(calls) >= 4, what="reopen")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await ctl.stop()

    run(body())


def test_knobs_unhealthy_for_a_while_raise_the_alarm(tmp_path: Path) -> None:
    from k1000 import __main__ as main_mod

    async def body() -> None:
        ctl, cl = make(tmp_path, State())
        fc = FakeControls()
        task = asyncio.get_running_loop().create_task(
            main_mod.watch_knobs(ctl, fc, period=0.01, alarm_s=0.1))
        await until(lambda: ctl.status.knobs_ok is True, what="ok")
        fc.healthy = False                       # a glitch the driver heals quickly: silent
        await asyncio.sleep(0.04)
        fc.healthy = True
        await asyncio.sleep(0.05)
        assert ctl.status.knobs_ok is True
        fc.healthy = False                       # a lasting fault: alarm
        await until(lambda: ctl.status.message == ctl_mod.MSG_KNOBS_OFFLINE, what="alarm")
        fc.healthy = True
        await until(lambda: ctl.status.message == "", what="cleared")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    run(body())


def test_open_controls_closes_a_half_opened_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    from k1000 import __main__ as main_mod
    from k1000.hw import mcp23017

    closed: list[bool] = []

    class Broken:
        def start(self) -> None:
            raise mcp23017.ControlsError("Errno 121")

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(mcp23017, "ZynthianControls", Broken)
    assert main_mod.open_controls({}) is None and closed == [True]


def test_screen_is_retried_until_the_panel_appears(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from k1000 import __main__ as main_mod

    class FakeFb:
        def __init__(self) -> None:
            self.frames = 0

        def show(self, img: Any) -> bool:
            self.frames += 1
            return True

    async def body() -> None:
        ctl, cl = make(tmp_path, State(), playing=True)
        await boot(ctl)
        fb = FakeFb()
        calls: list[bool] = []

        def fake_open(quiet: bool = False) -> Any:
            calls.append(quiet)
            return fb if len(calls) >= 3 else None

        monkeypatch.setattr(main_mod, "open_screen", fake_open)
        hw: dict[str, Any] = {"fb": None}
        task = asyncio.get_running_loop().create_task(main_mod.screen_task(ctl, hw, retry_s=0.02))
        await until(lambda: fb.frames >= 1, timeout=10.0, what="frames once the panel appeared")
        assert calls[:3] == [False, True, True] and hw["fb"] is fb
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await ctl.stop()

    run(body())


def test_open_screen_takes_only_the_panel_never_an_hdmi_fb0(monkeypatch) -> None:
    """Before the panel registers, fb0 may be vc4's HDMI framebuffer: never take it (BOOT-3)."""
    from k1000 import __main__ as main_mod
    from k1000.ui import display

    opened: list[str] = []
    monkeypatch.setattr(display, "find_fb_device", lambda *a, **k: None)
    monkeypatch.setattr(display, "Framebuffer", lambda dev, *a, **k: opened.append(dev))
    assert main_mod.open_screen() is None and main_mod.open_screen(quiet=True) is None
    assert opened == []


def test_render_loop_sleeps_while_nothing_changes_and_wakes_for_expiry(tmp_path: Path) -> None:
    from k1000 import __main__ as main_mod

    class FakeFb:
        def __init__(self) -> None:
            self.t: list[float] = []

        def show(self, img: Any) -> bool:
            self.t.append(time.monotonic())
            return True

    async def body() -> None:
        ctl, cl = make(tmp_path, State(), playing=True)
        await boot(ctl)
        fb = FakeFb()
        task = asyncio.get_running_loop().create_task(main_mod.render_loop(ctl, fb))
        await until(lambda: len(fb.t) >= 1, timeout=10.0, what="first frame")
        await asyncio.sleep(0.3)                 # warm-up done; drain anything pending
        n = len(fb.t)
        await asyncio.sleep(0.8)                 # nothing changes: no frame at meter rate
        assert len(fb.t) - n <= 1
        t0 = time.monotonic()
        ctl.show_hint("x", 0.2)                  # a frame now, another when it expires
        await until(lambda: any(t > t0 + 0.2 for t in fb.t), timeout=2.0, what="expiry frame")
        assert min(t for t in fb.t if t > t0 + 0.2) < t0 + 0.6
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await ctl.stop()

    run(body())


def test_render_loop_rewrites_a_static_frame_now_and_then(tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    from k1000 import __main__ as main_mod
    monkeypatch.setattr(main_mod, "FORCE_WRITE_S", 0.3)
    monkeypatch.setattr(main_mod, "RENDER_IDLE_S", 0.1)

    class FakeFb:
        def __init__(self) -> None:
            self.invalidated = 0

        def show(self, img: Any) -> bool:
            return True

        def invalidate(self) -> None:
            self.invalidated += 1

    async def body() -> None:
        ctl, cl = make(tmp_path, State(), playing=True)
        await boot(ctl)
        fb = FakeFb()
        task = asyncio.get_running_loop().create_task(main_mod.render_loop(ctl, fb))
        await until(lambda: fb.invalidated >= 2, timeout=10.0, what="periodic rewrite")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await ctl.stop()

    run(body())


def test_console_injects_knob_events_and_highlights_nudges(tmp_path: Path,
                                                            capsys: pytest.CaptureFixture[str]) -> None:
    from k1000.__main__ import run_command

    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-20.0), playing=True)
        await boot(ctl)
        stop = asyncio.Event()
        await run_command(ctl, "nudge master_db 1", stop)
        assert ctl.focus.editing_now() == "master_db"
        await run_command(ctl, "page LIVE", stop)
        await run_command(ctl, "ev enc 0 2", stop)                  # E1 on LIVE: slot +2
        assert ctl.focus.slot == 2
        await run_command(ctl, "ev sw 3 press", stop)               # E4 push held = FINE
        assert ctl.focus.held == {3}
        await run_command(ctl, "ev sw 5 long", stop)                # B2 long
        assert ctl.state.muted is True
        await run_command(ctl, "ev sw 5 sideways", stop)
        out = [l for l in capsys.readouterr().out.splitlines() if l.startswith("@")]
        assert out[-1].startswith("@err") and all(l.startswith("@ok") for l in out[:-1])
        await ctl.stop()

    run(body())


def test_sigterm_keeps_the_screen_live_until_the_controller_stopped(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stop during a ramp: the panel must follow the audio until stop() returns (it froze on
    'MUTED' for 2.6 s while Main ramped up), then show the stop frame."""
    from k1000 import __main__ as main_mod

    frames: list[tuple[float, Any]] = []
    window: dict[str, float] = {}

    class FakeFb:
        def show(self, img: Any) -> bool:
            frames.append((time.monotonic(), img))
            return True

        def close(self) -> None:
            pass

    client = FakeClient(dc.generate(DEV, State()))
    monkeypatch.setattr(main_mod, "CamillaClient", lambda url, **kw: client)
    monkeypatch.setattr(main_mod, "open_screen", lambda quiet=False: FakeFb())
    real_stop = Controller.stop

    async def slow_stop(self: Controller, *a: Any, **kw: Any) -> None:
        window["start"] = time.monotonic()
        self.show_hint("stopping", 5.0)          # something visible changes during stop()
        await asyncio.sleep(0.4)
        await real_stop(self, *a, **kw)
        window["end"] = time.monotonic()

    monkeypatch.setattr(Controller, "stop", slow_stop)
    settings = tmp_path / "k1000.yaml"
    settings.write_text(yaml.safe_dump({"profile": "dev"}))
    args = main_mod.build_parser().parse_args([
        "--settings", str(settings), "--state", str(tmp_path / "state.json"),
        "--active-config", str(tmp_path / "active.yml"), "--no-hw"])

    async def body() -> int:
        async def stopper() -> None:
            await until(lambda: len(frames) >= 1, timeout=10.0, what="first frame")
            await asyncio.sleep(0.3)
            os.kill(os.getpid(), signal.SIGTERM)
        t = asyncio.get_running_loop().create_task(stopper())
        code = await main_mod.run(args)
        await t
        return code

    assert run(body()) == 0
    during = [t for t, _ in frames if window["start"] < t < window["end"]]
    assert during, "no frame drawn while stop() ran"
    assert frames[-1][0] >= window["end"]        # the stop frame comes last
