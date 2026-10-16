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
import shutil
import socket
import struct
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
from k1000.cdsp import CamillaClient, CdspError, CdspNoConfig, CdspNotApplied, CdspUnavailable
from k1000.cdsp import _matches as cdsp_matches
from k1000.control import (
    MARKER_DB, MARKER_FADER, Controller, EngineSettings, Paths, Status, decode_marker, logical_levels,
    marker_db, recover_state,
)
from k1000.hw.mcp23017 import EncoderEvent, SwitchEvent
from k1000.ui.input_map import Toggle
from k1000.profiles import ProfileChoice, load_profile
from k1000.state import RANGES, State, delay_samples

BOX = Path(__file__).resolve().parents[1]
# The engine on PATH first (a dev machine: a wrapper running the Pi's binary), like test_dspconfig.
CDSP_BIN = shutil.which("camilladsp") or "/usr/local/bin/camilladsp"
ENGINE_PORT = int(os.environ.get("K1000_CONTROL_TEST_PORT", "12346"))


def _is_wrapper(path: str) -> bool:
    """CDSP_BIN is a script (not the ELF binary): e.g. the Pi's binary run under qemu in docker."""
    try:
        with open(path, "rb") as f:
            return f.read(4) != b"\x7fELF"
    except OSError:
        return False


_CDSP_WRAPPED = _is_wrapper(CDSP_BIN)


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
# Config keys the engine stores as f32 (camilladsp-schema LoudnessParameters: FiniteF32).
F32_KEYS = frozenset({"reference_level", "low_boost", "high_boost"})


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
        # The real client confirms with GetConfigValue, and 5.0.0-beta.1 keeps the Loudness
        # boosts and reference as f32: it reads them back widened (6.1 -> 6.099999904632568;
        # GetConfigJson, below, prints the short form). The same confirmation as CamillaClient.
        if pointer.rsplit("/", 1)[-1] in F32_KEYS and isinstance(value, float):
            got = struct.unpack("<f", struct.pack("<f", value))[0]
            if not cdsp_matches(got, value):
                raise CdspNotApplied("SetConfigValue", "NotApplied",
                                     f"accepted, but the active config still differs: {got!r}")
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
# k1000.yaml once the Knob test confirmed B1..B4 (D-014's gate on the B3 hold)
CONFIRMED: dict[str, Any] = {"controls": {"buttons_confirmed": True}}


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
        # Lo-Cut off (SETUP > TOPS > Lo-Cut): a frequency move to 10 Hz in click-safe steps,
        # never a rebuild
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
        # SETUP > Reset live EQ: smoothed back to flat
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
        for _ in range(12):                 # 12 detents in 60 ms (a fast DELAY turn, ALIGN E2)
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
        # confirmed (SETUP choose + push since D-013): Main muted around the upload, then back
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


def test_stop_lets_a_debounced_save_in_flight_land_before_its_own(tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """A debounced save writes from a thread. stop() cancels the persist task, but not that
    thread, so its result was never recorded: stop()'s save_now then compared the newest content
    with a stale record, found nothing new, and the late thread write (older content) landed
    LAST, in state.json and active.yml alike. Seen as a flaky D-016 test: a switch turned off
    came back on after a restart (an AUX mic open again). Here the thread is held until stop()
    runs, and the newest edit equals the last recorded save: stop() must still leave it on disk."""
    real = ctl_mod.atomic_write
    armed, gate, held = threading.Event(), threading.Event(), threading.Event()

    def slow(path: Any, data: Any, mode: int = 0o644) -> None:
        if armed.is_set() and threading.current_thread() is not threading.main_thread():
            held.set()
            gate.wait(5.0)
        real(path, data, mode)

    monkeypatch.setattr(ctl_mod, "atomic_write", slow)

    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-20.0), playing=True,
                       persist_debounce_s=0.05, persist_max_delay_s=0.2)
        await boot(ctl)
        await asyncio.sleep(0.3)                    # the boot saves are done
        armed.set()
        ctl.nudge("live_peq.0.gain", -6)            # a -3 dB notch: its save is held in the thread
        await until(held.is_set, what="a debounced save in flight")
        ctl.nudge("live_peq.0.gain", 6)             # back to 0 dB: the content last recorded
        asyncio.get_running_loop().call_later(0.2, gate.set)
        await ctl.stop()
        gate.set()
        await asyncio.sleep(0.3)                    # any late write has landed by now
        assert json.loads((tmp_path / "state.json").read_text())["live_peq"][0]["gain"] == 0.0
        assert yaml.safe_load((tmp_path / "active.yml").read_text())[
            "filters"]["live_peq_1"]["parameters"]["gain"] == 0.0

    run(body())


# ======================================================================= UI plumbing
def _sw_ev(i: int, kind: str) -> SwitchEvent:
    return SwitchEvent(i, kind, time.monotonic())


def _enc_ev(i: int, d: int) -> EncoderEvent:
    return EncoderEvent(i, d, time.monotonic())


def _setup_to(ctl: Controller, field: str) -> None:
    from k1000.ui import input_map as im
    ctl.focus.item = next(i for i, it in enumerate(im.SETUP_ITEMS) if it.field == field)


def _push_twice(ctl: Controller) -> None:
    for _ in range(2):
        ctl.handle_event(_sw_ev(0, "press"))
        ctl.handle_event(_sw_ev(0, "short"))


def test_knob_and_button_events_through_input_map(tmp_path: Path) -> None:
    """The v1 show, gesture by gesture (D-009, buttons D-013: B1 PAGE tap/hold, B2 BAND/UNLOCK,
    B3 NOTCH, B4 MUTE): SHOW <-> ALIGN on a B1 tap, SETUP on a B1 hold, and home."""
    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-20.0), playing=True, settings=CONFIRMED)
        await boot(ctl)
        assert ctl.page == "SHOW"
        ctl.handle_event(_enc_ev(0, 1))                               # E1 = master
        assert ctl.state.master_db == -19.5
        ctl.handle_event(_sw_ev(7, "short"))                          # B4 short: hint only
        assert ctl.focus.hint == "HOLD B4 TO MUTE / UNMUTE"
        assert ctl.state.muted is False
        ctl.handle_event(_sw_ev(7, "long"))                           # B4 long: mute
        assert ctl.state.muted is True
        await until(lambda: cl.faders[0]["mute"] is True, what="mute reaches the engine")
        ctl.handle_event(_sw_ev(7, "long"))                           # B4 long again: unmute
        await until(lambda: cl.faders[0]["mute"] is False, what="unmute")
        ctl.handle_event(_sw_ev(5, "short"))                          # B2: next band P1 -> P2
        assert ctl.focus.slot == 1 and ctl.focus.editing_now() == "slot"
        assert ctl.state.muted is False                               # B2 is not the mute any more
        ctl.handle_event(_sw_ev(5, "long"))                           # B2 hold on SHOW: nothing
        assert ctl.focus.slot == 1 and ctl.state.muted is False and ctl.page == "SHOW"
        ctl.handle_event(_enc_ev(3, -2))                              # E4 = gain of PEQ 2 (D-015)
        assert ctl.state.live_peq[1].gain == -1.0
        ctl.handle_event(_sw_ev(6, "long"))                           # B3 hold: P2 OUT (D-014),
        assert ctl.state.live_peq[1].gain == -1.0 and ctl.state.live_peq[1].on is False   # no reset
        assert ctl.focus.hint_now() == "P2 OUT"
        ctl.handle_event(_sw_ev(6, "long"))                           # again: back IN, as it was
        assert ctl.state.live_peq[1].gain == -1.0 and ctl.state.live_peq[1].on is True
        assert ctl.focus.hint_now() == "P2 IN"
        ctl.handle_event(_sw_ev(4, "press"))                          # B1 press: nothing yet
        assert ctl.page == "SHOW"
        ctl.handle_event(_sw_ev(4, "short"))                          # B1 tap: ALIGN
        assert ctl.page == "ALIGN"
        hz = ctl.state.lowcut_hz
        ctl.handle_event(_enc_ev(3, 2))                               # E4 locked: hint only
        assert ctl.state.lowcut_hz == hz and ctl.focus.hint_now() == "LOCKED · HOLD B2 TO EDIT"
        ctl.handle_event(_sw_ev(6, "long"))                           # B3 hold on ALIGN: no unlock
        assert ctl.snapshot()[2].align_locked is True
        ctl.handle_event(_sw_ev(5, "long"))                           # B2 hold: unlock
        assert ctl.snapshot()[2].align_locked is False
        ctl.handle_event(_enc_ev(3, 2))
        assert ctl.state.lowcut_hz > hz
        ctl.handle_event(_sw_ev(4, "press"))
        ctl.handle_event(_sw_ev(4, "long"))                           # B1 hold: SETUP (relocks ALIGN)
        ctl.handle_event(_sw_ev(4, "release"))
        assert ctl.page == "SETUP" and ctl.snapshot()[2].align_locked is True
        _setup_to(ctl, "lowcut_on")
        ctl.handle_event(_sw_ev(0, "press"))
        ctl.handle_event(_sw_ev(0, "short"))                          # first push: chooses OFF only
        assert ctl.state.lowcut_on is True and ctl.focus.pick_now() == ("lowcut_on", False)
        ctl.handle_event(_sw_ev(0, "press"))
        ctl.handle_event(_sw_ev(0, "short"))                          # second push: Lo-Cut off
        assert ctl.state.lowcut_on is False and ctl.focus.pick_now() is None
        assert ctl.focus.hint_now() == "LO-CUT OFF" and ctl.focus.editing_now() == "lowcut_on"
        _setup_to(ctl, "reset_live")
        _push_twice(ctl)                                              # confirmed reset
        assert ctl.state.live_peq[1].gain == 0.0
        assert ctl.focus.hint_now() == ctl_mod.HINT_LIVE_RESET
        assert ctl.snapshot()[2].undo_reset_s > 290
        _setup_to(ctl, "undo_reset")
        ctl.handle_event(_sw_ev(0, "press"))
        ctl.handle_event(_sw_ev(0, "short"))                          # one push: undone
        assert ctl.state.live_peq[1].gain == -1.0 and ctl.focus.hint_now() == ctl_mod.HINT_LIVE_RESTORED
        ctl.handle_event(_sw_ev(4, "press"))                          # B1 press on SETUP: nothing
        assert ctl.page == "SETUP"
        ctl.handle_event(_sw_ev(4, "short"))                          # B1 tap: SETUP -> SHOW
        assert ctl.page == "SHOW"
        ctl.handle_event(_sw_ev(4, "short"))                          # -> ALIGN
        ctl.handle_event(_sw_ev(4, "short"))                          # -> SHOW (never SETUP)
        assert ctl.page == "SHOW"
        ctl.handle_event(_sw_ev(4, "short"))                          # -> ALIGN
        ctl.handle_event(_sw_ev(4, "long"))                           # B1 hold from ALIGN: SETUP
        assert ctl.page == "SETUP"
        ctl.handle_event(_sw_ev(4, "long"))                           # B1 hold on SETUP: home
        assert ctl.page == "SHOW"
        assert ctl.state.muted is False                               # no B1 gesture ever muted
        assert await ctl.settle(5.0)
        assert cl.cfg["filters"]["live_peq_2"]["parameters"]["gain"] == -1.0
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
        assert ctl.state.muted is True and ctl.page == "SHOW"          # v0's LIVE = SHOW
        status = json.loads(out[5].split(" ", 1)[1])
        assert status["engine_ready"] is True and status["page"] == "SHOW"
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
        evs = [EncoderEvent(0, 2, now), object(), SwitchEvent(7, "long", now + 0.01)]   # B4 long
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
        assert "(GUI)" in ctl.status.message
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


# ======================================================================= UI v1 (D-009)
class _Clock:
    """A settable UI clock (Controller._ui_clock)."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class _Frame:
    def __init__(self, peak_hz: float | None) -> None:
        self.peak_hz = peak_hz


def _gain_writes(log: list[tuple[Any, ...]]) -> list[Any]:
    return [v for i in range(1, 5) for v in sets_for(log, f"/filters/live_peq_{i}/parameters/gain")]


def test_master_up_guard_caps_rises_per_window(tmp_path: Path) -> None:
    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-30.0), playing=True)
        await boot(ctl)
        clk = _Clock()
        ctl._ui_clock = clk
        assert ctl.master_up_guard_db == 6.0 and ctl_mod.MASTER_UP_WINDOW_S == 2.0
        for _ in range(12):                                     # +6 dB in one window: fine
            assert ctl.nudge("master_db", 1) is not None
            clk.t += 0.1
        assert ctl.state.master_db == -24.0
        assert ctl.nudge("master_db", 1) is None                # the 13th: refused, said
        assert ctl.state.master_db == -24.0 and ctl.focus.hint_now() == ctl_mod.HINT_MASTER_SLOW
        assert ctl.nudge("master_db", -4) == -26.0              # down: always instant
        assert ctl.nudge("master_db", 1) is None                # down does not buy room
        clk.t += 1.0                                            # 3 raises (1.5 dB) left the window
        assert ctl.nudge("master_db", 4) == -24.5               # 3 of the 4 detents
        clk.t += 10.0
        assert ctl.nudge("master_db", 20) == -18.5              # a sweep: only the first 6 dB
        assert ctl.focus.hint_now() == ctl_mod.HINT_MASTER_SLOW
        # the knob path is the same (and never accelerated up)
        clk.t += 10.0
        for _ in range(16):
            ctl.handle_event(EncoderEvent(0, 1, time.monotonic()))
        assert ctl.state.master_db == -12.5
        # a clamp at 0 dB costs only what was raised
        ctl2, _ = make(tmp_path / "b", State(master_db=-2.0), playing=True)
        assert ctl2.nudge("master_db", 1000) == 0.0 and ctl2.focus.hint_now() is None
        assert await ctl.settle(5.0)
        assert max(faders_sent(cl.log, 0)) <= -12.5
        await ctl.stop()

    run(body())


def test_master_up_guard_is_configurable_and_reaches_the_phone(tmp_path: Path) -> None:
    from k1000.web.server import PhoneBridge

    async def body() -> None:
        off, _ = make(tmp_path / "off", State(master_db=-40.0), playing=True,
                      settings={"controls": {"master_up_guard_db": 0}})
        assert off.master_up_guard_db == 0.0
        assert off.nudge("master_db", 60) == -10.0               # off: as v0
        assert ctl_mod.master_up_guard_db({"controls": {"master_up_guard_db": "x"}}) == 6.0
        assert ctl_mod.master_up_guard_db({"controls": {"master_up_guard_db": True}}) == 6.0
        assert ctl_mod.master_up_guard_db({"controls": {"master_up_guard_db": 99}}) == 24.0
        assert ctl_mod.master_up_guard_db({}) == 6.0
        ctl, _ = make(tmp_path / "on", State(master_db=-40.0), playing=True,
                      settings={"controls": {"master_up_guard_db": 3}})
        clk = _Clock()
        ctl._ui_clock = clk
        b = PhoneBridge(ctl, master_up_db_per_s=100.0)
        oks = [b.apply({"t": "nudge", "f": "master_db", "d": 2})[0] for _ in range(4)]
        assert oks == [True, True, True, False] and ctl.state.master_db == -37.0
        assert b.apply({"t": "nudge", "f": "master_db", "d": -1})[0]

    run(body())


def test_notch_helper_takes_a_free_bell_at_the_peak_and_never_touches_gain(tmp_path: Path) -> None:
    async def body() -> None:
        st = State()
        st.live_peq[0].gain = -4.0                    # P1 in use (at 250 Hz)
        ctl, cl = make(tmp_path, st, playing=True)
        await boot(ctl)
        ctl.focus.slot = 2                            # P3 selected and free: preferred
        ctl.set_spectrum(_Frame(2440.0), "live")
        n = len(cl.log)
        assert ctl.notch_helper() == 2
        b = ctl.state.live_peq[2]
        assert b.freq == 2440.0 and b.q == pytest.approx(8.651, abs=0.001) and b.gain == 0.0
        assert ctl.focus.slot == 2 and ctl.focus.editing_now() == "live_peq.2.gain"
        assert ctl.focus.hint_now() == "P3 · 2.44k · E4 TO CUT"
        assert await ctl.settle(5.0)
        assert _gain_writes(cl.log[n:]) == []         # inaudible: freq and Q only
        assert cl.cfg["filters"]["live_peq_3"]["parameters"]["freq"] == 2440.0
        assert ctl.state.live_peq[0].gain == -4.0
        # the same peak again: P3 is free (gain 0) and selected -> stays there
        assert ctl.notch_helper() == 2
        # a peak within 1/6 octave of a bell IN USE selects it and writes nothing
        ctl.nudge("live_peq.2.gain", -12)             # the operator cut it
        assert await ctl.settle(5.0)
        ctl.focus.slot = 5
        ctl.set_spectrum(_Frame(2600.0), "live")      # log2(2600/2440) = 0.09 oct
        before, n = ctl.state.to_dict(), len(cl.log)
        assert ctl.notch_helper() == 2
        assert ctl.state.to_dict() == before and ctl.focus.slot == 2
        assert ctl.focus.hint_now() == "P3 · 2.44k · E4 TO CUT"
        assert await ctl.settle(5.0)
        assert nomark(cl.log[n:]) == []
        # a peak elsewhere: the first free bell (never a shelf, even selected and flat)
        ctl.focus.slot = 4
        ctl.set_spectrum(_Frame(800.0), "live")
        assert ctl.notch_helper() == 1
        assert ctl.state.live_peq[1].freq == 800.0 and ctl.state.live_lowshelf.q == 0.707
        await ctl.stop()

    run(body())


def test_notch_helper_without_a_peak_or_a_free_band(tmp_path: Path) -> None:
    async def body() -> None:
        ctl, cl = make(tmp_path, State(), playing=True)
        await boot(ctl)
        ctl.focus.slot = 1
        assert ctl.notch_helper() == 1                # no spectrum: Q only, the freq stays
        assert ctl.state.live_peq[1].freq == 1000.0 and ctl.state.live_peq[1].q == pytest.approx(8.651, abs=1e-3)
        assert ctl.focus.hint_now() == "P2 · 1.00k · NO PEAK · E4 TO CUT"
        ctl.set_spectrum(_Frame(3000.0), "live")
        ctl.state.spectrum_on = False                 # SETUP > Spectrum OFF: no peak target
        ctl.focus.slot = 3
        assert ctl.notch_helper() == 3 and ctl.state.live_peq[3].freq == 5000.0
        ctl.state.spectrum_on = True
        for i in range(4):
            ctl.set_value(f"live_peq.{i}.gain", -3.0)
        ctl.focus.slot = 0
        ctl.set_spectrum(_Frame(100.0), "live")       # far from every bell in use
        before = ctl.state.to_dict()
        assert ctl.notch_helper() is None
        assert ctl.focus.hint_now() == ctl_mod.HINT_NO_FREE_BAND
        assert ctl.state.to_dict() == before and ctl.focus.slot == 0
        await ctl.stop()

    run(body())


def test_notch_helper_from_b3_on_show(tmp_path: Path) -> None:
    """D-013: the notch helper is B3 (sw6), no longer B4 (sw7, now the mute)."""
    async def body() -> None:
        ctl, _ = make(tmp_path, State(), playing=True)
        await boot(ctl)
        ctl.set_spectrum(_Frame(125.0), "live")
        freq = ctl.state.live_peq[0].freq
        ctl.handle_event(SwitchEvent(7, "short", time.monotonic()))   # B4 tap: the mute's hint
        assert ctl.state.live_peq[0].freq == freq
        assert ctl.focus.hint_now() == "HOLD B4 TO MUTE / UNMUTE"
        ctl.handle_event(SwitchEvent(6, "short", time.monotonic()))   # B3 tap: the notch
        assert ctl.state.live_peq[0].freq == 125.0 and ctl.state.live_peq[0].gain == 0.0
        ctl.goto_page("ALIGN")
        ctl.set_spectrum(_Frame(4000.0), "live")
        ctl.handle_event(SwitchEvent(6, "short", time.monotonic()))   # off SHOW: hint only
        assert ctl.state.live_peq[0].freq == 125.0
        assert ctl.focus.hint_now() == "B3: NOTCH HELPER ON SHOW"
        await ctl.stop()

    run(body())


def test_reset_live_keeps_a_one_level_undo(tmp_path: Path) -> None:
    async def body() -> None:
        st = State()
        st.live_peq[1].freq, st.live_peq[1].gain, st.live_peq[1].q = 2520.0, -6.5, 8.0
        st.live_highshelf.gain = 2.0
        ctl, cl = make(tmp_path, st, playing=True)
        await boot(ctl)
        clk = _Clock()
        ctl._ui_clock = clk
        before = ctl.state.live_peq[1], ctl.state.live_highshelf
        want = ctl_mod._live_dict(ctl.state)
        assert ctl.undo_left() == 0.0 and ctl.undo_reset() is False
        assert ctl.focus.hint_now() == ctl_mod.HINT_NOTHING_TO_UNDO
        ctl.reset_live()
        assert ctl.state.live_peq[1].gain == 0.0 and ctl.undo_left() == pytest.approx(300.0)
        assert await ctl.settle(5.0)
        clk.t += 299.0
        assert ctl.undo_reset() is True
        assert ctl_mod._live_dict(ctl.state) == want and ctl.focus.hint_now() == ctl_mod.HINT_LIVE_RESTORED
        assert await ctl.settle(5.0)
        p = cl.cfg["filters"]["live_peq_2"]["parameters"]
        assert (p["freq"], p["gain"], p["q"]) == (2520.0, -6.5, 8.0)
        assert ctl.undo_reset() is False              # one level
        # expires after 5 min
        ctl.reset_live()
        clk.t += 300.0
        assert ctl.undo_left() == 0.0 and ctl.undo_reset() is False
        # ...and at the next live EQ edit, from anywhere
        ctl.undo_reset()
        ctl.state = State.from_dict({**ctl.state.to_dict(), **want})
        ctl.reset_live()
        ctl.nudge("live_peq.0.gain", -1)
        assert ctl.undo_left() == 0.0
        ctl.nudge("live_peq.0.gain", 1)               # flat again: still gone
        assert ctl.undo_left() == 0.0
        assert before                                 # (kept for the reader)
        await ctl.stop()

    run(body())


def test_reset_undo_survives_inaudible_edits_and_says_when_it_is_lost(tmp_path: Path) -> None:
    """LAS-4 / UX-2: B3 (the notch) right after a mistaken reset (the reflex to find the ring
    again) moves a free bell's Q/freq at 0 dB: inaudible, so the undo that brings the notches back
    stays. The first audible edit (a cut) ends it, and the screen says so."""
    async def body() -> None:
        st = State()
        st.live_peq[1].freq, st.live_peq[1].gain, st.live_peq[1].q = 2520.0, -6.5, 8.0
        st.live_peq[3].freq, st.live_peq[3].gain, st.live_peq[3].q = 6300.0, -4.0, 4.0
        ctl, cl = make(tmp_path, st, playing=True)
        await boot(ctl)
        clk = _Clock()
        ctl._ui_clock = clk
        want = ctl_mod._live_dict(ctl.state)
        ctl.reset_live()
        assert ctl.focus.hint_now() == ctl_mod.HINT_LIVE_RESET
        # B3 on SHOW: no peak -> P1's Q (inaudible); then with a peak -> P1 at the peak
        ctl.focus.slot = 0
        assert ctl.notch_helper() == 0 and ctl.state.live_peq[0].gain == 0.0
        assert ctl.undo_left() == pytest.approx(300.0)
        ctl.set_spectrum(_Frame(2440.0), "live")
        assert ctl.notch_helper() == 0 and ctl.state.live_peq[0].freq == 2440.0
        ctl.nudge("live_peq.2.freq", 7)               # E2 / E3 on a flat band
        ctl.nudge("live_peq.2.q", -3)
        ctl.nudge("live_highshelf.freq", 2)
        assert ctl.undo_left() == pytest.approx(300.0)
        assert ctl.snapshot()[2].undo_reset_s == pytest.approx(300.0)
        # undo: the notches from before the reset, over the inaudible moves
        assert ctl.undo_reset() is True and ctl_mod._live_dict(ctl.state) == want
        assert await ctl.settle(5.0)
        p = cl.cfg["filters"]["live_peq_4"]["parameters"]
        assert (p["freq"], p["gain"], p["q"]) == (6300.0, -4.0, 4.0)
        # the first audible edit ends it, said on the screen
        ctl.reset_live()
        ctl.notch_helper()
        ctl.nudge("live_peq.0.gain", -1)              # E4: the cut
        assert ctl.focus.hint_now() == ctl_mod.HINT_UNDO_LOST == "RESET UNDO LOST"
        assert ctl.undo_left() == 0.0 and ctl.undo_reset() is False
        # an expiry is silent
        ctl.reset_live()
        clk.t += 300.0
        ctl.show_hint("X")
        assert ctl.undo_left() == 0.0 and ctl.focus.hint_now() == "X"
        await ctl.stop()

    run(body())


def test_every_master_change_is_logged_with_its_source(tmp_path: Path, caplog) -> None:
    """LAS-3: the 20:25 Main -19 -> -20 could not be attributed: no line said what moved it."""
    import logging

    from k1000.web.server import PhoneBridge

    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-19.0), playing=True)
        await boot(ctl)
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="k1000.control"):
            ctl.handle_event(EncoderEvent(0, -1, time.monotonic()))         # E1 on SHOW
            PhoneBridge(ctl).apply({"t": "nudge", "f": "master_db", "d": -2})
            ctl.set_value("master_db", -25.0, source="console")
            ctl.nudge("live_peq.0.gain", -1)                                # not master: no line
            assert await ctl.settle(5.0)
            ctl._own_fader.clear()
            cl.faders[0]["volume"] = -26.5                                  # CamillaGUI
            await ctl.poll_once()
        lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("master ")]
        assert lines == ["master -19.0 -> -19.5 dB (knob)", "master -19.5 -> -20.5 dB (phone)",
                         "master -20.5 -> -25.0 dB (console)",
                         "master -25.0 -> -26.5 dB (adoption)"]
        await ctl.stop()

    run(body())


def test_phone_reset_and_lo_cut_go_through_the_box_paths(tmp_path: Path) -> None:
    from k1000.web.server import PhoneBridge

    async def body() -> None:
        st = State()
        st.live_peq[2].gain = -9.0
        ctl, cl = make(tmp_path, st, playing=True)
        await boot(ctl)
        b = PhoneBridge(ctl)
        assert b.apply({"t": "reset_live", "confirm": True})[0]
        assert ctl.state.live_peq[2].gain == 0.0 and ctl.undo_left() > 0   # the box can undo it
        assert ctl.undo_reset() and ctl.state.live_peq[2].gain == -9.0
        assert b.apply({"t": "set", "f": "lowcut_on", "v": False})[:2] == (True, False)
        assert ctl.state.lowcut_on is False and ctl.focus.hint_now() == "LO-CUT OFF"
        assert "LO-CUT OFF" in ctl.snapshot()[2].pills
        assert await ctl.settle(5.0)
        assert cl.cfg["filters"]["top_lowcut"]["parameters"]["freq"] == 10.0     # parked, no rebuild
        assert not any(e[0] == "config" for e in cl.log)
        assert b.apply({"t": "set", "f": "lowcut_on", "v": True})[:2] == (True, True)
        assert ctl.focus.hint_now().startswith("LO-CUT ON")
        await ctl.stop()

    run(body())


def test_align_lock_relocks_when_leaving_and_after_idle(tmp_path: Path) -> None:
    async def body() -> None:
        ctl, _ = make(tmp_path, State(sub_delay_ms=1.0), playing=True)
        await boot(ctl)
        ctl.goto_page("ALIGN")
        ctl.handle_event(SwitchEvent(5, "long", time.monotonic()))   # B2 hold (D-013): unlock
        snap = ctl.snapshot()[2]
        assert snap.align_locked is False and 29.0 < snap.align_unlock_s <= 30.0
        ctl.goto_page("SHOW")
        ctl.goto_page("ALIGN")
        assert ctl.snapshot()[2].align_locked is True             # leaving ALIGN relocked
        ctl.handle_event(EncoderEvent(1, 3, time.monotonic()))    # E2 = DELAY (D-015)
        assert ctl.state.sub_delay_ms == 1.0
        ctl.handle_event(SwitchEvent(5, "long", time.monotonic()))
        ctl.focus.unlock_until = time.monotonic() - 0.01          # 30 s of idle went by
        ctl.handle_event(EncoderEvent(1, 3, time.monotonic()))
        assert ctl.state.sub_delay_ms == 1.0 and ctl.snapshot()[2].align_locked is True
        await ctl.stop()

    run(body())


def test_align_and_setup_go_home_after_60_s_without_input(tmp_path: Path) -> None:
    async def body() -> None:
        ctl, _ = make(tmp_path, State(), playing=True)
        await boot(ctl)
        clk = _Clock()
        ctl._ui_clock = clk
        for page in ("ALIGN", "SETUP"):
            ctl.goto_page(page)
            clk.t += 59.0
            ctl.ui_tick()
            assert ctl.page == page
            clk.t += 1.0
            ctl.ui_tick()
            assert ctl.page == "SHOW", page
        ctl.goto_page("ALIGN")
        clk.t += 50.0
        ctl.handle_event(EncoderEvent(0, -1, time.monotonic()))   # input renews the minute
        clk.t += 59.0
        ctl.ui_tick()
        assert ctl.page == "ALIGN"
        clk.t += 1.0
        ctl.ui_tick()
        assert ctl.page == "SHOW"
        for page in ("PHONE", "HWTEST", "SHOW"):                  # these stay
            ctl.goto_page(page)
            clk.t += 600.0
            ctl.ui_tick()
            assert ctl.page == page
        ctl.goto_page("SETUP")                                    # a pending confirm holds it
        ctl.focus.confirm, ctl.focus.confirm_until = "mono_subs", clk.t + 100.0
        clk.t += 61.0
        ctl.ui_tick(clk.t)
        assert ctl.page == "SETUP"
        await ctl.stop()

    run(body())


def test_snapshot_carries_the_v1_render_contract(tmp_path: Path) -> None:
    async def body() -> None:
        st = State(lowcut_on=False, sub_inverted=True, top_inverted=True, mono_subs=True,
                   dev_audition="tops")
        ctl, _ = make(tmp_path, st, playing=True)
        await boot(ctl)
        ctl.pill_sources.append(lambda: "LAPTOP OPEN 1:59")
        ctl.pill_sources.append(lambda: None)
        ctl.pill_sources.append(lambda: 1 / 0)                   # a broken source is skipped
        ctl.focus.slot = 4
        page, _state, status, focus = ctl.snapshot()
        assert page == status.page == "SHOW"
        assert (status.band, status.band_label, status.band_short) == (4, "LO SHELF", "LS")
        assert status.pills == ["LAPTOP OPEN 1:59", "LO-CUT OFF", "POL INV SUB+TOP", "MONO SUB",
                                "AUDITION TOPS"]
        assert status.align_locked is True and status.align_unlock_s == 0.0
        assert status.alerts == [] and status.sys_alert == "" and status.sys_alert_level == ""
        assert status.undo_reset_s == 0.0 and status.spectrum_on is True
        assert status.spectrum is None and status.spectrum_state == "off"
        assert status.master_up_guard_db == 6.0
        # alerts: KNOBS OFFLINE (red) first, then ENGINE DOWN, then a warning
        ctl._set_message("drift", 2, "PIPELINE CHANGED (GUI)")
        ctl.set_knobs_ok(False)
        ctl.status.engine = "Stalled"
        status = ctl.snapshot()[2]
        assert status.alerts == [("critical", ctl_mod.MSG_KNOBS_OFFLINE),
                                 ("critical", "ENGINE STALLED"),
                                 ("warning", "PIPELINE CHANGED (GUI)")]
        assert (status.sys_alert_level, status.sys_alert) == ("critical", ctl_mod.MSG_KNOBS_OFFLINE)
        ctl.set_knobs_ok(True)
        ctl.status.engine, ctl.status.device_present = None, False
        assert [a[1] for a in ctl.snapshot()[2].alerts][:2] == ["NO DEVICE · HiFiBerry", "ENGINE DOWN"]
        ctl.status.engine, ctl.status.device_present = "Running", True
        ctl._set_message("ramp", 3, "RAMPING IN")
        assert ("info", "RAMPING IN") in ctl.snapshot()[2].alerts
        # copies are independent
        a = ctl.snapshot()[2]
        a.pills.append("x")
        a.alerts.clear()
        assert "x" not in ctl.status.pills and ctl.status.alerts
        await ctl.stop()

    run(body())


def test_spectrum_hooks_wanted_and_publish(tmp_path: Path) -> None:
    class FakeRta:
        def __init__(self) -> None:
            self.inputs = 0

        def note_input(self) -> None:
            self.inputs += 1

    async def body() -> None:
        ctl, _ = make(tmp_path, State(), playing=True)
        await boot(ctl)
        await ctl.poll_once()
        ctl.rta = FakeRta()
        assert ctl.spectrum_wanted() is True
        v = ctl.version
        ctl.changed.clear()
        ctl.set_spectrum(_Frame(1234.0), "live")
        assert ctl.version == v and ctl.changed.is_set()          # the screen only, not phones
        snap = ctl.snapshot()[2]
        assert snap.peak_hz == 1234.0 and snap.spectrum_state == "live" and snap.spectrum is not None
        ctl.handle_event(EncoderEvent(1, 1, time.monotonic()))
        assert ctl.rta.inputs == 1                                # the next frame waits for the edit
        ctl.goto_page("ALIGN")
        assert ctl.spectrum_wanted() is False
        ctl.goto_page("SHOW")
        ctl.handle_action(Toggle("spectrum_on"))
        assert ctl.state.spectrum_on is False and ctl.focus.hint_now() == "SPECTRUM OFF"
        assert ctl.spectrum_wanted() is False and ctl.snapshot()[2].spectrum_on is False
        ctl.handle_action(Toggle("spectrum_on"))
        ctl.status.engine = "Stalled"
        assert ctl.spectrum_wanted() is False
        ctl.status.engine = "Paused"
        assert ctl.spectrum_wanted() is True
        ctl.status.connected = False
        assert ctl.spectrum_wanted() is False
        # k1000.yaml spectrum.enabled: false
        off, _ = make(tmp_path / "off", State(), playing=True, settings={"spectrum": {"enabled": False}})
        assert off.spectrum_enabled is False and off.spectrum_wanted() is False
        snap = off.snapshot()[2]
        assert snap.spectrum_state == "disabled" and snap.spectrum_on is False
        off.handle_action(Toggle("spectrum_on"))
        off.handle_action(Toggle("spectrum_on"))
        assert off.focus.hint_now() == ctl_mod.HINT_SPECTRUM_DISABLED
        assert ctl_mod.spectrum_enabled({"spectrum": {"enabled": "no"}}) is True   # only `false`
        await ctl.stop()

    run(body())


def _rta_frame(level: float, state_peak: float | None = None) -> Any:
    np = pytest.importorskip("numpy")
    from k1000.ui.eqgraph import RtaFrame
    f = np.geomspace(10.0, 20000.0, 264)
    return RtaFrame(f, np.full(264, float(level)), np.full(264, float(level)), -126.0, -60.0,
                    state_peak)


def test_identical_spectrum_frames_do_not_wake_the_screen(tmp_path: Path) -> None:
    """Silence gives the same frame 10 times a second: it must not keep SHOW rendering at the
    stream rate (the idle rate is ~1 frame/s). Any change of picture or state wakes it."""
    async def body() -> None:
        ctl, _ = make(tmp_path, State(), playing=True)
        await boot(ctl)
        woke = []
        for frame, state in ((_rta_frame(-140.0), "live"),      # first frame: wakes
                             (_rta_frame(-140.0), "live"),      # same picture (new arrays)
                             (_rta_frame(-100.0), "live"),      # louder: wakes
                             (_rta_frame(-100.0, 2440.0), "live"),   # a PEAK: wakes
                             (_rta_frame(-100.0, 2440.0), "waiting"),  # state: wakes
                             (None, "off"),                      # cleared: wakes
                             (None, "off")):                     # still nothing: no wake
            ctl.changed.clear()
            v = ctl.version
            ctl.set_spectrum(frame, state)
            woke.append(ctl.changed.is_set())
            assert ctl.version == v                              # never the phones' version
        assert woke == [True, False, True, True, True, True, False]
        assert ctl_mod._same_spectrum(object(), object()) is False
        ctl._ui_clock = lambda: 1234.5
        ctl.handle_event(EncoderEvent(1, 1, time.monotonic()))
        assert ctl.last_input == 1234.5
        await ctl.stop()

    run(body())


def test_render_loop_draws_a_moving_spectrum_at_the_stream_rate(tmp_path: Path) -> None:
    """The spectrum (30 new frames/s here) and the meters wake the loop independently: SHOW
    still draws at most RENDER_STREAM_FPS, from real pages.render frames with the spectrum."""
    pytest.importorskip("PIL")
    from k1000 import __main__ as main_mod

    class FakeFb:
        def __init__(self) -> None:
            self.t: list[float] = []
            self.last: Any = None

        def show(self, img: Any) -> bool:
            self.t.append(time.monotonic())
            self.last = img
            return True

    async def body() -> None:
        ctl, cl = make(tmp_path, State(), playing=True, meter_s=0.05)
        await boot(ctl)

        class FakeRta:
            def note_input(self) -> None:
                pass

        ctl.rta = FakeRta()                     # a spectrum task is running
        fb = FakeFb()
        task = asyncio.get_running_loop().create_task(main_mod.render_loop(ctl, fb))
        try:
            await until(lambda: len(fb.t) >= 1, timeout=10.0, what="first frame")
            await asyncio.sleep(0.3)
            n0, t0 = len(fb.t), time.monotonic()
            for i in range(45):
                cl.peaks = [-20.0 + (i % 5), -18.0]
                ctl.set_spectrum(_rta_frame(-100.0 + (i % 9)), "live")
                await asyncio.sleep(1 / 30)
            rate = (len(fb.t) - n0) / (time.monotonic() - t0)
            assert 3.0 <= rate <= main_mod.RENDER_STREAM_FPS + 2, rate
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await ctl.stop()
        from k1000.ui import pages
        assert fb.last.size == pages.SIZE
        g = pages.GRAPH                         # the spectrum is drawn behind the curve
        from k1000.ui.eqgraph import STYLE
        assert any(fb.last.getpixel((x, g[3] - 3)) == STYLE.rta_fill for x in range(60, 400, 7))

    run(body())


def test_setup_action_items_run_registered_callables(tmp_path: Path) -> None:
    from k1000.ui.input_map import SetupAction

    async def body() -> None:
        ctl, _ = make(tmp_path, State(), playing=True)
        await boot(ctl)
        ran: list[str] = []
        ctl.handle_action(SetupAction("laptop_access"))
        assert ctl.focus.hint_now() == "LAPTOP_ACCESS: NOT AVAILABLE"
        ctl.setup_actions["laptop_access"] = lambda: ran.append("x")
        ctl.handle_action(SetupAction("laptop_access"))
        ctl.setup_actions["boom"] = lambda: 1 / 0
        ctl.handle_action(SetupAction("boom"))
        assert ran == ["x"] and ctl.focus.hint_now() == "BOOM FAILED (SEE LOG)"
        await ctl.stop()

    run(body())


def test_spectrum_on_is_persisted_and_old_state_files_load(tmp_path: Path) -> None:
    async def body() -> None:
        ctl, _ = make(tmp_path, State(), playing=True)
        await boot(ctl)
        ctl.toggle("spectrum_on")
        await ctl.stop()
        assert json.loads((tmp_path / "state.json").read_text())["spectrum_on"] is False
        old = State().to_dict()
        old.pop("spectrum_on")
        assert State.from_dict(old).spectrum_on is True

    run(body())


def test_main_wiring_of_the_spectrum_and_the_v1_console(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                        capsys: pytest.CaptureFixture[str]) -> None:
    from k1000 import __main__ as main_mod
    from k1000.__main__ import _next_wake, run_command
    from k1000.ui.input_map import Focus

    f = Focus(unlock_until=10.4)
    assert _next_wake(f, 10.0) == pytest.approx(0.41)            # the EDIT pill's end redraws
    assert _next_wake(Focus(pick="mono_subs", pick_value=True, pick_until=10.3), 10.0) == \
        pytest.approx(0.31)                                       # a lapsing SETUP choice too (D-013)
    made: list[tuple[str, dict[str, Any]]] = []

    class OwnClient:
        connected = False

        def __init__(self, url: str, **kw: Any) -> None:
            made.append((url, kw))

        async def connect(self, timeout: Any = None) -> bool:
            return False

        async def close(self) -> None:
            pass

    async def body() -> None:
        ctl, cl = make(tmp_path, State(), playing=True)
        await boot(ctl)
        monkeypatch.setattr(main_mod, "CamillaClient", OwnClient)
        task = main_mod.open_spectrum(ctl, "ws://127.0.0.1:4321")
        assert task is not None and task.wanted == ctl.spectrum_wanted
        assert task.publish == ctl.set_spectrum and list(task.channels()) == [0, 1]
        await ctl.poll_once()
        await until(lambda: made, what="its own client")
        assert made[0][0] == "ws://127.0.0.1:4321" and made[0][1]["timeout"] <= 0.5
        assert task.client is not cl
        assert task.ballistics.peak_floor_db == -80.0 and task.program_floor_db == -60.0
        await task.stop()
        tuned, _ = make(tmp_path / "tuned", State(),
                        settings={"spectrum": {"peak_floor_db": -72.5, "program_floor_db": -55}})
        task2 = main_mod.open_spectrum(tuned, "ws://x")     # k1000.yaml's PEAK floors reach it
        assert task2.ballistics.peak_floor_db == -72.5 and task2.program_floor_db == -55.0
        await task2.stop()
        off, _ = make(tmp_path / "off", State(), settings={"spectrum": {"enabled": False}})
        assert main_mod.open_spectrum(off, "ws://x") is None
        # console: notch helper and undo
        stop = asyncio.Event()
        ctl.set_value("live_peq.3.gain", -5.0)
        for line in ("notch", "reset_live", "undo_reset", "undo_reset", "page ALIGN", "status"):
            await run_command(ctl, line, stop)
        out = [l for l in capsys.readouterr().out.splitlines() if l.startswith("@")]
        assert out[0].startswith("@ok") and json.loads(out[0].split(" ", 1)[1])["slot"] == 0
        assert out[2].startswith("@ok") and out[3].startswith("@err")      # one level of undo
        assert ctl.state.live_peq[3].gain == -5.0
        status = json.loads(out[5].split(" ", 1)[1])
        assert status["page"] == "ALIGN" and status["align_locked"] is True
        assert status["spectrum"] is None and status["pills"] == [] and status["alerts"] == []
        ctl.rta = task                                            # (stopped; only its frame)
        ctl.set_spectrum(rta_frame(), "live")
        await run_command(ctl, "status", stop)
        st = json.loads(capsys.readouterr().out.strip().split(" ", 1)[1])
        assert st["spectrum"]["bins"] == 3 and st["spectrum"]["peak_hz"] == 100.0
        await ctl.stop()

    run(body())


def rta_frame() -> Any:
    import numpy as np
    from k1000.ui.eqgraph import RtaFrame
    return RtaFrame(np.array([50.0, 100.0, 200.0]), np.array([-60.0, -20.0, -60.0]), None,
                    -84.0, -18.0, 100.0)


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
            if _CDSP_WRAPPED:
                # A wrapper script (docker run): SIGKILL would kill the docker client and leave
                # the engine running in its container (port busy); SIGTERM is proxied to it.
                self.proc.terminate()
                try:
                    self.proc.wait(10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            else:
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


def test_b4_during_a_held_boot_ramp_keeps_main_muted(tmp_path: Path) -> None:
    """Engine not processing yet, K1000 holds Main muted before its ramp: the screen says Main
    comes in by itself and B4 keeps it muted; B4 long then MUTES (the ramp is cancelled into a
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
        ctl.handle_event(SwitchEvent(7, "long", time.monotonic()))   # B4 long
        assert ctl.state.muted is True
        await asyncio.sleep(0.05)
        assert ("mute", 0, False) not in cl.log[n:]
        assert _band(ctl) == ctl_mod.MSG_HELD_MUTED          # the reason, not READY
        cl.engine_state = "Running"
        await until(lambda: ctl.engine_ready and not ctl.ramping, what="ramp finished")
        assert ("mute", 0, False) not in cl.log
        assert cl.faders[0]["mute"] is True and ctl.displayed_muted()
        assert _band(ctl) == "READY · hold B4 to unmute"
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
    ctl._set_message("held", 1, "TOPS GROUP -3.0 dB (GUI)")
    assert ctl.status.message and ctl.status.alert == ""
    ctl._set_message("setup", 4, "APPLYING SETUP…")
    assert ctl.status.alert == "APPLYING SETUP…"


def test_edits_toward_silence_are_saved_at_once(tmp_path: Path) -> None:
    """A power cut right after B4 (or after pulling the master down) must not boot the PA back
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
        # a setup rebuild too (the D-016 switches are not read back from a running engine: a
        # k1000 dying before the save would strip them from the engine on its restart)
        assert ctl.set_choice("deharsh_on", True)
        await until(lambda: saved()["deharsh_on"] is True, timeout=1.0, what="rebuild saved")
        assert dc.DH_COMP in yaml.safe_load((tmp_path / "active.yml").read_text())["processors"]
        await ctl.stop()                                # ...and saved on the way out
        assert saved()["master_db"] == -13.0

    run(body())


def test_a_master_only_save_does_not_rebuild_active_yml(tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """PERF-3: MASTER is a fader, not in active.yml: its urgent saves (every 0.25 s while it
    goes down) write the state JSON only, without dumping the YAML in the event loop."""
    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-10.0), playing=True)
        await boot(ctl)
        calls = []
        real = dc.to_yaml
        monkeypatch.setattr(dc, "to_yaml", lambda cfg, *a: calls.append(1) or real(cfg, *a))
        sp, ap = tmp_path / "state.json", tmp_path / "active.yml"
        yml = ap.read_text()
        for _ in range(5):
            ctl.nudge("master_db", -2)
            await asyncio.sleep(0.3)
        await until(lambda: json.loads(sp.read_text())["master_db"] == -15.0, what="saved")
        assert calls == [] and ap.read_text() == yml
        ctl.nudge("live_peq.0.gain", -2)                # a config change: dumped, saved
        await until(lambda: "gain: -1.0" in ap.read_text(), what="active.yml")
        assert len(calls) == 1
        await ctl.stop()

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


def test_input_peaks_wake_the_screen_on_show_and_align(tmp_path: Path) -> None:
    """D-015: SHOW and ALIGN draw the program input (IN L R): a visible change of its capture
    peaks wakes the render loop (`changed`), never `version` (the phones' state does not carry
    it); a change under one meter step wakes nothing, and no change does on a page without the
    meters."""
    async def body() -> None:
        ctl, cl = make(tmp_path, State(), playing=True)
        await boot(ctl)
        for page in ("SHOW", "ALIGN"):
            ctl.goto_page(page)
            cl.cap_peaks = [-30.0, -30.0]
            await ctl.meter_once()
            ctl.changed.clear()
            v = ctl.version
            cl.cap_peaks = [-30.05, -29.95]                       # under one meter step
            await ctl.meter_once()
            assert not ctl.changed.is_set(), page
            cl.cap_peaks = [-12.0, -14.0]
            await ctl.meter_once()
            assert ctl.changed.is_set() and ctl.version == v, page
            assert ctl.snapshot()[2].in_peak_db == [-12.0, -14.0]
        ctl.goto_page("SETUP")
        ctl.changed.clear()
        cl.cap_peaks = [-6.0, -6.0]
        await ctl.meter_once()
        assert not ctl.changed.is_set()                           # SETUP draws no meter
        assert ctl.snapshot()[2].in_peak_db == [-6.0, -6.0]       # (stored all the same)
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
        fc.queue.put_nowait(SwitchEvent(7, "long", time.monotonic()))      # B4 works
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
        await run_command(ctl, "page SHOW", stop)
        await run_command(ctl, "ev sw 5 short", stop)               # B2 on SHOW: next band x2
        await run_command(ctl, "ev sw 5 short", stop)
        assert ctl.focus.slot == 2 and ctl.state.muted is False
        await run_command(ctl, "ev sw 3 press", stop)               # E4 push held = FINE
        assert ctl.focus.held == {3}
        await run_command(ctl, "ev sw 7 long", stop)                # B4 long
        assert ctl.state.muted is True
        await run_command(ctl, "ev sw 7 sideways", stop)
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


# ======================================================================= D-011
SUB_DELAY_PTR = "/filters/sub_delay/parameters/delay"
TOP_DELAY_PTR = "/filters/top_delay/parameters/delay"
LOWCUT_PTR = "/filters/top_lowcut/parameters/freq"


def _config_writes(log: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    return [e for e in log if e[0] in ("set", "patch", "config")]


def _engine_delays(cl: FakeClient) -> tuple[float, float]:
    f = cl.cfg["filters"]
    return f["sub_delay"]["parameters"]["delay"], f["top_delay"]["parameters"]["delay"]


def test_signed_delay_one_engine_update_per_click_and_the_late_side_dips(tmp_path: Path) -> None:
    """ALIGN E2 (D-011): every click is ONE config write, under a dip of the group whose delay
    changes (subs = Aux1, tops = Aux2); crossing zero stops on ALIGNED; the engine never holds
    both delays after a move."""
    async def body() -> None:
        ctl, cl = make(tmp_path, State(sub_delay_ms=0.2), playing=True)
        await boot(ctl)

        async def click(d: int, fine: bool = False) -> list[tuple[Any, ...]]:
            n = len(cl.log)
            ctl.nudge("align_delay_ms", d, fine)
            assert await ctl.settle(3.0)
            ops = nomark(cl.log[n:])
            assert len(_config_writes(ops)) == 1, ops
            sub, top = _engine_delays(cl)
            assert sub == 0.0 or top == 0.0, (sub, top)
            return ops

        assert await click(-1) == [("mute", 1, True), ("set", SUB_DELAY_PTR, dc.delay_on_grid(0.1)),
                                   ("mute", 1, False)]
        assert await click(-1) == [("mute", 1, True), ("set", SUB_DELAY_PTR, 0.0), ("mute", 1, False)]
        assert ctl.state.get("align_delay_ms") == 0.0                    # ALIGNED: one click
        assert await click(-1) == [("mute", 2, True), ("set", TOP_DELAY_PTR, dc.delay_on_grid(0.1)),
                                   ("mute", 2, False)]                   # now the TOPS dip
        assert (ctl.state.sub_delay_ms, ctl.state.top_delay_ms) == (0.0, 0.1)
        ops = await click(1, fine=True)                                  # fine: one sample
        assert sets_for(ops, TOP_DELAY_PTR) == [dc.delay_on_grid(0.1 - 1 / 48)]
        assert delay_samples(_engine_delays(cl)[1]) == delay_samples(0.1) - 1
        # a move that does not change the sample count writes nothing and dips nothing
        n = len(cl.log)
        ctl.set_value("align_delay_ms", ctl.state.get("align_delay_ms") - 0.004)
        assert await ctl.settle(3.0)
        assert nomark(cl.log[n:]) == []
        assert cl.faders[1]["mute"] is False and cl.faders[2]["mute"] is False
        await ctl.stop()

    run(body())


def test_signed_delay_crossing_in_one_update_dips_both_groups(tmp_path: Path) -> None:
    """Two clicks the worker sends as one plan (tops 0.1 -> ALIGNED -> subs 0.1): both delays
    change in ONE PatchConfig, and both groups are muted before it and released after it."""
    async def body() -> None:
        ctl, cl = make(tmp_path, State(top_delay_ms=0.1), playing=True)
        await boot(ctl)
        n = len(cl.log)
        ctl.nudge("align_delay_ms", 1)
        ctl.nudge("align_delay_ms", 1)
        assert (ctl.state.sub_delay_ms, ctl.state.top_delay_ms) == (0.1, 0.0)
        assert await ctl.settle(3.0)
        ops = nomark(cl.log[n:])
        writes = _config_writes(ops)
        assert len(writes) == 1 and writes[0][0] == "patch", ops
        i = ops.index(writes[0])
        assert sorted(ops[:i]) == [("mute", 1, True), ("mute", 2, True)]
        assert sorted(ops[i + 1:]) == [("mute", 1, False), ("mute", 2, False)]
        assert sets_for(ops, SUB_DELAY_PTR) == [dc.delay_on_grid(0.1)]
        assert sets_for(ops, TOP_DELAY_PTR) == [0.0]
        assert _engine_delays(cl) == (dc.delay_on_grid(0.1), 0.0)
        await ctl.stop()

    run(body())


def test_signed_delay_normalises_a_saved_pair_on_its_first_move_never_at_boot(tmp_path: Path) -> None:
    async def body() -> None:
        ctl, cl = make(tmp_path, State(sub_delay_ms=1.0, top_delay_ms=0.5), playing=True)
        await boot(ctl)
        assert _config_writes(cl.log) == []                              # boot changes nothing
        assert ctl.state.get("align_delay_ms") == 0.5
        assert pages_align_value(ctl) == ("0.50", "SUBS LATER · 0.17 m")
        n = len(cl.log)
        ctl.nudge("align_delay_ms", 1)
        assert await ctl.settle(3.0)
        ops = nomark(cl.log[n:])
        assert len(_config_writes(ops)) == 1                             # both values, one patch
        assert sorted(op for op in ops if op[0] == "mute") == [
            ("mute", 1, False), ("mute", 1, True), ("mute", 2, False), ("mute", 2, True)]
        assert _engine_delays(cl) == (dc.delay_on_grid(0.6), 0.0)
        assert (ctl.state.sub_delay_ms, ctl.state.top_delay_ms) == (0.6, 0.0)
        await ctl.stop()

    run(body())


def pages_align_value(ctl: Controller) -> tuple[str, str]:
    from k1000.ui import pages
    return pages.fmt_align_delay(pages.align_delay(ctl.snapshot()[1]))


def test_signed_delay_through_the_knobs_lock_and_wake_unchanged(tmp_path: Path) -> None:
    async def body() -> None:
        ctl, cl = make(tmp_path, State(sub_delay_ms=0.1), playing=True)
        await boot(ctl)
        ctl.goto_page("ALIGN")
        ctl.handle_event(_enc_ev(1, -1))                       # E2 = DELAY (D-015): locked, hint only
        assert ctl.state.sub_delay_ms == 0.1 and ctl.focus.hint_now() == "LOCKED · HOLD B2 TO EDIT"
        ctl.handle_event(_sw_ev(5, "long"))                              # B2 hold: unlock
        assert ctl.focus.hint_now() == "DELAY + LO-CUT UNLOCKED"
        ctl.handle_event(_enc_ev(1, -1))                                 # the wake-up detent
        assert ctl.state.sub_delay_ms == 0.1 and ctl.focus.hint_now() == "DELAY · TURN AGAIN TO CHANGE"
        assert ctl.focus.editing_now() == "align_delay_ms"
        for want in ((0.0, 0.0), (0.0, 0.1), (0.0, 0.2)):              # ALIGNED, then the tops
            n = len(cl.log)
            ctl.handle_event(_enc_ev(1, -1))
            assert (ctl.state.sub_delay_ms, ctl.state.top_delay_ms) == want
            assert await ctl.settle(3.0)
            assert len(_config_writes(cl.log[n:])) == 1
        assert _engine_delays(cl) == (0.0, dc.delay_on_grid(0.2))
        ctl.handle_event(_sw_ev(5, "short"))                             # B2 tap: locked again
        ctl.handle_event(_enc_ev(1, 1))
        assert ctl.state.top_delay_ms == 0.2
        ctl.handle_event(_enc_ev(2, 1))                                  # E3 = BALANCE: live
        assert ctl.state.balance_db == 0.5 and ctl.state.top_delay_ms == 0.2
        await ctl.stop()

    run(body())


def test_lowcut_click_is_2_hz_and_an_off_grid_value_snaps_on_its_first_move(tmp_path: Path) -> None:
    async def body() -> None:
        ctl, cl = make(tmp_path, State(lowcut_hz=112.25), playing=True)
        await boot(ctl)
        assert _config_writes(cl.log) == []                              # nothing rewritten at boot
        assert cl.cfg["filters"]["top_lowcut"]["parameters"]["freq"] == 112.25
        for d, fine, want in ((1, False, 114.0), (-1, False, 112.0), (1, True, 113.0),
                              (4, False, 120.0), (-1, False, 118.0)):
            n = len(cl.log)
            assert ctl.nudge("lowcut_hz", d, fine) == want
            assert await ctl.settle(3.0)
            ops = cl.log[n:]
            assert sets_for(ops, LOWCUT_PTR)[-1] == want
            assert not any(e[0] == "mute" for e in ops)                  # a Lo-Cut move never dips
        await ctl.stop()

    run(body())


def test_knob_tags_reach_the_screen_status(tmp_path: Path, caplog: Any) -> None:
    async def body() -> None:
        for settings, want in (({}, "dim"), ({"ui": {"knob_tags": "off"}}, "off"),
                               ({"ui": {"knob_tags": False}}, "off"),
                               ({"ui": {"knob_tags": "bright"}}, "bright"),
                               ({"ui": {"knob_tags": "neon"}}, "dim")):
            ctl, _ = make(tmp_path, State(), settings=settings)
            assert ctl.snapshot()[2].knob_tags == want, settings
    with caplog.at_level("WARNING"):
        run(body())
    assert "knob_tags 'neon'" in caplog.text


# ======================================================================= D-013: SETUP choose-then-set
def _m4_ctl(tmp_path: Path, **kw: Any) -> Controller:
    """A Controller on the m4 profile (no audition mixer), not started: enough for SETUP rules."""
    return Controller(FakeClient(None), load_profile("m4", {}, probe=False), State(),
                      Paths(tmp_path / "s.json", tmp_path / "a.yml"), **kw)


def _push(ctl: Controller, n: int = 0) -> None:
    """E1 (n=0) or E2 (n=1) push, released without a turn: a SETUP 'select'."""
    ctl.handle_event(_sw_ev(n, "press"))
    ctl.handle_event(_sw_ev(n, "short"))


def test_setup_readonly_names_what_cannot_change_here(tmp_path: Path) -> None:
    """Controller.setup_readonly(): Profile while k1000.yaml pins it, Audition off the dev
    profile, Spectrum while k1000.yaml disables it; refreshed into Focus.readonly by every
    snapshot and every knob event."""
    # dev (the HiFiBerry: both inputs are the program): no spare input for AUX IN (D-016)
    aux = {"aux_mode": ctl_mod.HINT_AUX_NO_INPUT, "aux_gain_db": ctl_mod.HINT_AUX_NO_INPUT}
    assert ctl_mod.HINT_AUX_NO_INPUT == "AUX: NO SPARE INPUT ON THIS DEVICE"
    ctl, _ = make(tmp_path / "a", State())
    assert ctl.setup_readonly() == aux and ctl.snapshot()[3].readonly == aux
    pinned, _ = make(tmp_path / "b", State(), profile_choice=ProfileChoice("dev", "settings"))
    assert pinned.setup_readonly() == {"profile": ctl_mod.HINT_PINNED, **aux}
    assert ctl_mod.HINT_PINNED == "PROFILE PINNED IN k1000.yaml"
    off, _ = make(tmp_path / "c", State(), settings={"spectrum": {"enabled": False}})
    assert off.setup_readonly() == {"spectrum_on": ctl_mod.HINT_SPECTRUM_DISABLED, **aux}
    assert _m4_ctl(tmp_path / "d").setup_readonly() == {
        "dev_audition": ctl_mod.HINT_AUDITION_DEV_ONLY}
    assert ctl_mod.HINT_AUDITION_DEV_ONLY == "AUDITION: DEV PROFILE ONLY"
    gig = _m4_ctl(tmp_path / "e", settings={"spectrum": {"enabled": False}},
                  profile_choice=ProfileChoice("m4", "settings"))
    want = {"profile": ctl_mod.HINT_PINNED, "dev_audition": ctl_mod.HINT_AUDITION_DEV_ONLY,
            "spectrum_on": ctl_mod.HINT_SPECTRUM_DISABLED}
    assert gig.setup_readonly() == want
    assert gig.snapshot()[3].readonly == want
    gig.focus.readonly = {}
    gig.handle_event(_enc_ev(0, 1))                       # any knob event refreshes it
    assert gig.focus.readonly == want


def test_set_choice_rebuild_field_goes_under_a_main_mute_and_says_no_change(tmp_path: Path) -> None:
    """SetChoice -> Controller.set_choice: a rebuild field (Mono, Audition) rebuilds as the old
    confirmed toggle did (Main muted around the upload); a value already held changes nothing
    and says so; only a field's own choices are ever set."""
    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-10.0), playing=True)
        await boot(ctl)
        n = len(cl.log)
        assert ctl.set_choice("mono_subs", True) is True
        assert await ctl.settle(5.0)
        ops = nomark(cl.log[n:])
        assert [e[0] for e in ops] == ["mute", "config", "mute"]
        assert ops[0] == ("mute", 0, True) and ops[2] == ("mute", 0, False)
        assert len(ops[1][1]["mixers"]["split"]["mapping"][0]["sources"]) == 2
        assert ctl.state.mono_subs is True and cl.faders[MARKER_FADER]["volume"] == MARKER_DB
        n = len(cl.log)
        assert ctl.set_choice("mono_subs", True) is False             # already set: nothing
        assert ctl.focus.hint_now() == ctl_mod.HINT_NO_CHANGE == "NO CHANGE: ALREADY SET"
        n2 = len(cl.log)
        assert ctl.set_choice("dev_audition", "subs") is True
        assert await ctl.settle(5.0)
        assert [e[0] for e in nomark(cl.log[n2:])] == ["mute", "config", "mute"]
        assert ctl.state.dev_audition == "subs"
        # bools are coerced; anything that is not one of the field's choices is refused
        assert ctl.set_choice("top_inverted", 1) is True and ctl.state.top_inverted is True
        assert ctl.set_choice("dev_audition", "both") is False and ctl.state.dev_audition == "subs"
        assert ctl.set_choice("profile", "nope") is False and ctl.state.profile == "dev"
        assert ctl.set_choice("master_db", 0.0) is False and ctl.state.master_db == -10.0
        assert await ctl.settle(5.0)
        assert [e for e in cl.log[n:n2] if e[0] == "config"] == []
        await ctl.stop()
        saved = json.loads((tmp_path / "state.json").read_text())
        assert saved["mono_subs"] is True and saved["dev_audition"] == "subs"

    run(body())


def test_set_choice_profile_refused_while_pinned(tmp_path: Path) -> None:
    """k1000.yaml pins the profile: neither set_choice nor the knobs (E2 turn, push) switch it;
    each says why and nothing is chosen."""
    from k1000.ui import input_map as im

    async def body() -> None:
        ctl, cl = make(tmp_path, State(), playing=True,
                       profile_choice=ProfileChoice("dev", "settings"))
        await boot(ctl)
        n = len(cl.log)
        assert ctl.set_choice("profile", "m4") is False
        assert ctl.focus.hint_now() == ctl_mod.HINT_PINNED and ctl.state.profile == "dev"
        ctl.goto_page("SETUP")
        _setup_to(ctl, "profile")
        ctl.focus.hint = None
        ctl.handle_event(_enc_ev(1, 1))                               # E2: no choice, the reason
        assert ctl.focus.hint_now() == ctl_mod.HINT_PINNED and ctl.focus.pick_now() is None
        ctl.focus.hint = None
        _push(ctl)                                                    # push: the same
        assert ctl.focus.hint_now() == ctl_mod.HINT_PINNED and ctl.focus.pick_now() is None
        assert ctl.state.profile == "dev" and im.SETUP_ITEMS[ctl.focus.item].field == "profile"
        assert await ctl.settle(3.0)
        assert not any(e[0] == "config" for e in cl.log[n:])
        await ctl.stop()

    run(body())


def test_set_choice_audition_refused_off_the_dev_profile(tmp_path: Path) -> None:
    """The m4 profile has no audition mixer: Audition (dev) cannot be set there, by set_choice
    or by the knobs."""
    ctl = _m4_ctl(tmp_path)
    assert ctl.set_choice("dev_audition", "tops") is False
    assert ctl.focus.hint_now() == ctl_mod.HINT_AUDITION_DEV_ONLY
    assert ctl.state.dev_audition == "sum"
    ctl.goto_page("SETUP")
    _setup_to(ctl, "dev_audition")
    ctl.focus.hint = None
    ctl.handle_event(_enc_ev(1, 1))
    assert ctl.focus.hint_now() == ctl_mod.HINT_AUDITION_DEV_ONLY and ctl.focus.pick_now() is None
    ctl.focus.hint = None
    _push(ctl, 1)
    assert ctl.focus.hint_now() == ctl_mod.HINT_AUDITION_DEV_ONLY and ctl.focus.pick_now() is None
    assert ctl.state.dev_audition == "sum"


def test_set_choice_spectrum_refused_when_disabled_and_said_when_set(tmp_path: Path) -> None:
    async def body() -> None:
        off, _ = make(tmp_path / "off", State(), playing=True,
                      settings={"spectrum": {"enabled": False}})
        await boot(off)
        assert off.set_choice("spectrum_on", False) is False
        assert off.focus.hint_now() == ctl_mod.HINT_SPECTRUM_DISABLED
        assert off.state.spectrum_on is True                         # untouched
        off.goto_page("SETUP")
        _setup_to(off, "spectrum_on")
        off.focus.hint = None
        off.handle_event(_enc_ev(1, -1))
        assert off.focus.hint_now() == ctl_mod.HINT_SPECTRUM_DISABLED and off.focus.pick_now() is None
        await off.stop()
        ctl, _ = make(tmp_path / "on", State(), playing=True)
        await boot(ctl)
        assert ctl.set_choice("spectrum_on", False) is True
        assert ctl.focus.hint_now() == "SPECTRUM OFF" and ctl.spectrum_wanted() is False
        assert ctl.set_choice("spectrum_on", True) is True
        assert ctl.focus.hint_now() == "SPECTRUM ON" and ctl.state.spectrum_on is True
        await ctl.stop()

    run(body())


def test_set_choice_lo_cut_goes_through_set_lowcut(tmp_path: Path) -> None:
    """Lo-Cut is not a plain state write: set_choice hands it to set_lowcut (the frequency
    parked at 10 Hz in click-safe steps, never a rebuild; said on the screen)."""
    async def body() -> None:
        ctl, cl = make(tmp_path, State(), playing=True)
        await boot(ctl)
        calls: list[bool] = []
        real = ctl.set_lowcut

        def spy(on: bool) -> bool:
            calls.append(on)
            return real(on)
        ctl.set_lowcut = spy                                          # type: ignore[method-assign]
        n = len(cl.log)
        assert ctl.set_choice("lowcut_on", False) is True
        assert calls == [False] and ctl.state.lowcut_on is False
        assert ctl.focus.hint_now() == "LO-CUT OFF" and ctl.focus.editing_now() == "lowcut_on"
        assert await ctl.settle(5.0)
        freqs = sets_for(cl.log[n:], LOWCUT_PTR)
        assert freqs[-1] == 10.0 and len(freqs) > 10
        assert not any(e[0] == "config" for e in cl.log[n:])
        assert ctl.set_choice("lowcut_on", 0) is False and calls == [False]   # already off
        assert ctl.focus.hint_now() == ctl_mod.HINT_NO_CHANGE
        assert ctl.set_choice("lowcut_on", True) is True and calls == [False, True]
        assert ctl.focus.hint_now().startswith("LO-CUT ON")
        await ctl.stop()

    run(body())


def test_a_choice_the_phone_made_meanwhile_is_no_change(tmp_path: Path) -> None:
    """E2 chose Lo-Cut OFF, a phone switched it off before the push: the push changes nothing
    and says so (never a flip back ON, never a second Lo-Cut move)."""
    from k1000.web.server import PhoneBridge

    async def body() -> None:
        ctl, cl = make(tmp_path, State(), playing=True)
        await boot(ctl)
        ctl.goto_page("SETUP")
        _setup_to(ctl, "lowcut_on")
        ctl.handle_event(_enc_ev(1, 1))                               # E2: chooses OFF
        assert ctl.focus.pick_now() == ("lowcut_on", False) and ctl.state.lowcut_on is True
        assert PhoneBridge(ctl).apply({"t": "set", "f": "lowcut_on", "v": False})[:2] == (True, False)
        assert await ctl.settle(5.0)
        n = len(cl.log)
        _push(ctl)                                                    # the push comes late
        assert ctl.state.lowcut_on is False and ctl.focus.pick_now() is None
        assert ctl.focus.hint_now() == ctl_mod.HINT_NO_CHANGE
        assert await ctl.settle(3.0)
        assert cl.log[n:] == []
        await ctl.stop()

    run(body())


def test_knobs_choose_then_push_through_the_controller(tmp_path: Path) -> None:
    """End to end on SETUP (handle_event hands map_event the State): E2 chooses from the value
    the state holds, the row waits, a push sets it. Mono L+R: off -> ON; Audition: SUM -> TOPS
    with one turn event, however many detents it carries."""
    async def body() -> None:
        ctl, cl = make(tmp_path, State(master_db=-10.0), playing=True)
        await boot(ctl)
        ctl.goto_page("SETUP")
        _setup_to(ctl, "mono_subs")
        n = len(cl.log)
        ctl.handle_event(_enc_ev(1, 1))                               # E2: choose ON
        assert ctl.focus.pick_now() == ("mono_subs", True) and ctl.state.mono_subs is False
        assert await ctl.settle(3.0)
        assert not any(e[0] == "config" for e in cl.log[n:])         # a turn alone never rebuilds
        _push(ctl)                                                    # E1 push: set
        assert ctl.state.mono_subs is True and ctl.focus.pick_now() is None
        assert ctl.focus.editing_now() == "mono_subs"
        assert await ctl.settle(5.0)
        assert [e[0] for e in nomark(cl.log[n:])] == ["mute", "config", "mute"]
        ctl.handle_event(_enc_ev(1, 1))                               # now from ON: chooses off
        assert ctl.focus.pick_now() == ("mono_subs", False)
        ctl.handle_event(_enc_ev(1, -1))                              # back on the value held
        assert ctl.focus.pick_now() is None
        _setup_to(ctl, "dev_audition")
        ctl.handle_event(_enc_ev(1, 3))                               # one event: one choice
        assert ctl.focus.pick_now() == ("dev_audition", "tops") and ctl.state.dev_audition == "sum"
        n = len(cl.log)
        _push(ctl, 1)                                                 # E2 push: set
        assert ctl.state.dev_audition == "tops" and ctl.focus.pick_now() is None
        assert await ctl.settle(5.0)
        assert [e[0] for e in nomark(cl.log[n:])] == ["mute", "config", "mute"]
        assert cl.cfg["mixers"]["output"]["mapping"][0]["sources"][0]["channel"] == 2
        await ctl.stop()

    run(body())


def test_a_page_change_drops_a_pending_choice(tmp_path: Path) -> None:
    """goto_page clears a chosen-but-not-set value: after a trip away (B1 tap to SHOW, hold back
    to SETUP) the next push proposes again, it never sets a stale choice."""
    async def body() -> None:
        ctl, cl = make(tmp_path, State(), playing=True)
        await boot(ctl)
        ctl.goto_page("SETUP")
        _setup_to(ctl, "mono_subs")
        ctl.handle_event(_enc_ev(1, 1))
        assert ctl.focus.pick_now() == ("mono_subs", True)
        ctl.goto_page("SETUP")                                        # same page: kept
        assert ctl.focus.pick_now() == ("mono_subs", True)
        ctl.handle_event(_sw_ev(4, "press"))
        ctl.handle_event(_sw_ev(4, "short"))                          # B1 tap: SHOW
        assert ctl.page == "SHOW" and ctl.focus.pick_now() is None
        ctl.handle_event(_sw_ev(4, "press"))
        ctl.handle_event(_sw_ev(4, "long"))                           # B1 hold: SETUP again
        assert ctl.page == "SETUP" and ctl.focus.pick_now() is None
        _push(ctl)                                                    # proposes, does not set
        assert ctl.focus.pick_now() == ("mono_subs", True) and ctl.state.mono_subs is False
        ctl.goto_page("PHONE")
        assert ctl.focus.pick_now() is None and ctl.state.mono_subs is False
        assert await ctl.settle(3.0)
        assert not any(e[0] == "config" for e in cl.log)
        await ctl.stop()

    run(body())


def test_auto_home_waits_for_a_pending_choice(tmp_path: Path) -> None:
    async def body() -> None:
        ctl, _ = make(tmp_path, State(), playing=True)
        await boot(ctl)
        clk = _Clock()
        ctl._ui_clock = clk
        ctl.goto_page("SETUP")
        ctl.focus.pick, ctl.focus.pick_value, ctl.focus.pick_until = "mono_subs", True, clk.t + 100.0
        clk.t += 61.0
        ctl.ui_tick(clk.t)
        assert ctl.page == "SETUP" and ctl.focus.pick_now(clk.t) == ("mono_subs", True)
        clk.t += 40.0                                                 # the choice lapsed
        ctl.ui_tick(clk.t)
        assert ctl.page == "SHOW" and ctl.focus.pick is None
        assert ctl.state.mono_subs is False
        await ctl.stop()

    run(body())


def test_next_page_follows_the_b1_tap(tmp_path: Path) -> None:
    """Controller.next_page (console `page next`, NextPage) is a B1 tap (input_map.B1_TAP):
    SHOW <-> ALIGN, SETUP -> SHOW, PHONE -> SETUP, anything else home; never SETUP from
    SHOW/ALIGN (a hold away)."""
    from k1000.ui import input_map as im
    ctl, _ = make(tmp_path, State())
    for start, want in (("SHOW", "ALIGN"), ("ALIGN", "SHOW"), ("SETUP", "SHOW"),
                        ("PHONE", "SETUP"), ("HWTEST", "SHOW")):
        ctl.goto_page(start)
        assert ctl.next_page() == want == ctl.page, start
        assert want == im.B1_TAP.get(start, im.HOME_PAGE)
    ctl.goto_page("SHOW")
    ctl.handle_action(im.NextPage())
    assert ctl.page == "ALIGN"
    ctl.handle_action(im.NextPage())
    assert ctl.page == "SHOW"


def test_set_choice_only_takes_setup_choice_rows(tmp_path: Path) -> None:
    """set_choice is the SETUP rows' path only (D-013 review): never `muted` (set_mute's, with its
    immediate engine mute and seen-generation guard), nor a number or an action."""
    async def body() -> None:
        ctl, cl = make(tmp_path, State(), playing=True)
        await boot(ctl)
        before = ctl.state.to_dict()
        for field, value in (("muted", True), ("muted", False), ("master_db", -3.0),
                             ("out_trim_db.0", 2.0), ("reset_live", True)):
            assert ctl.set_choice(field, value) is False, field
        assert ctl.state.to_dict() == before
        assert ctl.set_choice("sub_inverted", True) is True and ctl.state.sub_inverted is True

    run(body())


# ======================================================================= D-014 band IN/OUT
def _p2_cut_state() -> State:
    st = State()
    st.live_peq[1].freq, st.live_peq[1].gain, st.live_peq[1].q = 2520.0, -6.5, 8.0
    return st


P2_GAIN = "/filters/live_peq_2/parameters/gain"


def test_band_out_and_in_is_a_smoothed_gain_move_kept_through_adoption_and_saved(tmp_path: Path,
                                                                                 caplog) -> None:
    import logging

    async def body() -> None:
        ctl, cl = make(tmp_path, _p2_cut_state(), playing=True)
        await boot(ctl)
        n = len(cl.log)
        with caplog.at_level(logging.INFO, logger="k1000.control"):
            assert ctl.set_band_on(1, False, source="phone") is False
        assert ctl.state.live_peq[1].on is False
        b = ctl.state.live_peq[1]
        assert (b.freq, b.gain, b.q) == (2520.0, -6.5, 8.0)              # settings kept
        assert ctl.focus.hint_now() == "P2 OUT"
        assert "band P2 OUT (phone): 2520 Hz, -6.5 dB stored, Q 8.00" in caplog.text
        assert await ctl.settle(5.0)
        writes = sets_for(cl.log[n:], P2_GAIN)
        assert writes[-1] == 0.0 and len(writes) == 13                  # 0.5 dB steps, no jump
        prev = -6.5
        for v in writes:
            assert abs(v - prev) <= 0.5 + 1e-9
            prev = v
        assert not any(e[0] == "config" for e in cl.log[n:])            # never a rebuild
        p = cl.cfg["filters"]["live_peq_2"]["parameters"]
        assert (p["freq"], p["gain"], p["q"]) == (2520.0, 0.0, 8.0)
        # the 1 Hz adoption keeps the band OUT with its stored gain (the engine's 0 dB is OUT's)
        ctl._own_ptr.clear()
        await ctl.poll_once()
        assert ctl.state.live_peq[1].on is False and ctl.state.live_peq[1].gain == -6.5
        assert await ctl.settle(5.0)
        # the same request again changes nothing (an explicit value, never a blind toggle)
        n = len(cl.log)
        ctl.show_hint("X")
        assert ctl.set_band_on(1, False) is False and ctl.focus.hint_now() == "X"
        assert await ctl.settle(5.0)
        assert sets_for(cl.log[n:], P2_GAIN) == []
        # IN: the stored cut comes back, smoothed
        assert ctl.toggle_band(1) is True and ctl.focus.hint_now() == "P2 IN"
        assert await ctl.settle(5.0)
        assert sets_for(cl.log[n:], P2_GAIN)[-1] == -6.5 and len(sets_for(cl.log[n:], P2_GAIN)) == 13
        # saved: an OUT band survives a restart
        ctl.set_band_on(1, False)
        assert await ctl.settle(5.0)
        await ctl.stop()
        saved = json.loads((tmp_path / "state.json").read_text())
        assert saved["live_peq"][1] == {"freq": 2520.0, "gain": -6.5, "q": 8.0, "on": False}
        assert yaml.safe_load((tmp_path / "active.yml").read_text())["filters"]["live_peq_2"][
            "parameters"]["gain"] == 0.0

    run(body())


def test_band_on_refuses_garbage(tmp_path: Path) -> None:
    ctl, _ = make(tmp_path, State())
    before = ctl.state.to_dict()
    for slot, on in ((6, False), (-1, False), (True, False), ("1", False), (None, False), (1.0, False),
                     (1, "false"), (1, 0), (1, None)):
        assert ctl.set_band_on(slot, on) is None, (slot, on)
    for slot in (6, -1, True, "1"):
        assert ctl.toggle_band(slot) is None
    assert ctl.state.to_dict() == before


def test_b3_hold_on_show_switches_the_selected_band(tmp_path: Path) -> None:
    """D-014: B3 (sw6) held on SHOW = the selected band IN/OUT, hint P2 OUT / P2 IN; a tap is
    still the notch helper; off SHOW the hold does nothing."""
    async def body() -> None:
        ctl, cl = make(tmp_path, _p2_cut_state(), playing=True, settings=CONFIRMED)
        await boot(ctl)
        ctl.focus.slot = 1
        ctl.handle_event(SwitchEvent(6, "press", time.monotonic()))
        ctl.handle_event(SwitchEvent(6, "long", time.monotonic()))
        ctl.handle_event(SwitchEvent(6, "release", time.monotonic()))
        assert ctl.state.live_peq[1].on is False and ctl.focus.hint_now() == "P2 OUT"
        assert ctl.snapshot()[2].pills == ["OUT P2"]
        ctl.handle_event(SwitchEvent(6, "long", time.monotonic()))
        assert ctl.state.live_peq[1].on is True and ctl.focus.hint_now() == "P2 IN"
        ctl.focus.slot = 4                                              # a shelf, flat
        ctl.handle_event(SwitchEvent(6, "long", time.monotonic()))
        assert ctl.state.live_lowshelf.on is False and ctl.focus.hint_now() == "LS OUT"
        assert ctl.snapshot()[2].pills == ["OUT LS"]
        ctl.goto_page("ALIGN")
        before = ctl.state.to_dict()
        ctl.handle_event(SwitchEvent(6, "long", time.monotonic()))
        assert ctl.state.to_dict() == before
        # a knob moving a band that is OUT: stored, inaudible, and said
        ctl.goto_page("SHOW")
        ctl.handle_event(EncoderEvent(3, -2, time.monotonic()))        # E4 GAIN on LS
        assert ctl.state.live_lowshelf.gain == -1.0 and ctl.state.live_lowshelf.on is False
        assert ctl.focus.hint_now() == "LS OUT · HOLD B3 = IN"
        assert await ctl.settle(5.0)
        assert cl.cfg["filters"]["live_lowshelf"]["parameters"]["gain"] == 0.0
        await ctl.stop()

    run(body())


def test_b3_hold_is_only_a_hint_until_the_button_map_is_confirmed(tmp_path: Path, caplog: Any) -> None:
    """Review of D-014 (HIGH): with `controls: buttons` unconfirmed (the shipped k1000.yaml), a
    hold on what the map calls B3 changes nothing (it may be the B4 an operator holds to MUTE
    on a swapped map) and says which button it was; the journal says why at start. A band put
    OUT from the phone meanwhile is said to come back from the phone, not from B3. B4 still
    mutes; the B3 tap is still the notch helper."""
    import logging

    async def body() -> None:
        shipped = {"controls": {"encoders": [0, 1, 2, 3], "pushes": [0, 1, 2, 3],
                                "buttons": [4, 5, 6, 7]}}
        ctl, cl = make(tmp_path, _p2_cut_state(), playing=True, settings=shipped)
        assert "not confirmed" not in caplog.text                    # a demo/test object: quiet
        with caplog.at_level(logging.WARNING, logger="k1000.control"):
            await boot(ctl)
        assert "not confirmed" in caplog.text and "D-014" in caplog.text   # the journal says why
        ctl.focus.slot = 1
        before = ctl.state.to_dict()
        for k in ("press", "long", "release"):
            ctl.handle_event(SwitchEvent(6, k, time.monotonic()))
        assert ctl.state.to_dict() == before and ctl.state.live_peq[1].on is True
        assert ctl.focus.hint_now() == "B3 HOLD: IN/OUT AFTER KNOB TEST"
        assert ctl.pills() == []
        assert ctl.set_band_on(1, False, source="phone") is False     # the phone's chip hold
        ctl.handle_event(EncoderEvent(3, -1, time.monotonic()))        # E4 GAIN on P2 (OUT)
        assert ctl.focus.hint_now() == "P2 OUT · IN FROM THE PHONE"
        ctl.handle_event(SwitchEvent(7, "long", time.monotonic()))    # B4 hold: still the mute
        assert ctl.state.muted is True
        await ctl.stop()

    run(body())


def test_out_pills_name_every_band_out_in_band_order(tmp_path: Path) -> None:
    ctl, _ = make(tmp_path, State(lowcut_on=False, mono_subs=True))
    assert ctl.pills() == ["LO-CUT OFF", "MONO SUB"]
    ctl.set_band_on(4, False)
    ctl.set_band_on(1, False)
    assert ctl.pills() == ["LO-CUT OFF", "OUT P2 LS", "MONO SUB"]
    ctl.reset_live()                                                    # all IN again
    assert ctl.pills() == ["LO-CUT OFF", "MONO SUB"]


def test_notch_helper_reads_the_effective_gain_and_keeps_an_out_cut(tmp_path: Path) -> None:
    """D-014: a bell OUT is not in use (no reselect at its peak) and, with a stored cut, not free
    either (it keeps its settings); a flat bell OUT is free and comes back IN (inaudible)."""
    async def body() -> None:
        st = _p2_cut_state()
        st.live_peq[1].on = False                                       # P2 OUT, -6.5 at 2.52k
        st.live_peq[0].on = False                                       # P1 OUT, flat
        ctl, cl = make(tmp_path, st, playing=True)
        await boot(ctl)
        ctl.focus.slot = 1
        ctl.set_spectrum(_Frame(2440.0), "live")                        # P2's ring
        n = len(cl.log)
        assert ctl.notch_helper() == 0                                  # not P2: P1, free (0 dB)
        b0, b1 = ctl.state.live_peq[0], ctl.state.live_peq[1]
        assert b0.on is True and b0.freq == 2440.0 and b0.gain == 0.0
        assert (b1.on, b1.freq, b1.gain, b1.q) == (False, 2520.0, -6.5, 8.0)
        assert ctl.focus.hint_now() == "P1 · 2.44k · E4 TO CUT"
        assert await ctl.settle(5.0)
        assert _gain_writes(cl.log[n:]) == []                           # still silent
        # P2 back IN: now in use, so its peak selects it and writes nothing
        ctl.set_band_on(1, True)
        assert await ctl.settle(5.0)
        before = ctl.state.to_dict()
        ctl.focus.slot = 5
        assert ctl.notch_helper() == 1 and ctl.state.to_dict() == before
        # every bell either in use or OUT with a cut: no free band
        for i in (0, 2, 3):
            ctl.set_value(f"live_peq.{i}.gain", -3.0)
        ctl.set_band_on(3, False)
        ctl.set_spectrum(_Frame(100.0), "live")
        before = ctl.state.to_dict()
        assert ctl.notch_helper() is None and ctl.state.to_dict() == before
        assert ctl.focus.hint_now() == ctl_mod.HINT_NO_FREE_BAND
        await ctl.stop()

    run(body())


def test_switching_a_band_with_a_cut_ends_the_reset_undo_a_flat_one_does_not(tmp_path: Path) -> None:
    async def body() -> None:
        st = _p2_cut_state()
        ctl, _ = make(tmp_path, st, playing=True)
        await boot(ctl)
        clk = _Clock()
        ctl._ui_clock = clk
        want = ctl_mod._live_dict(ctl.state)
        ctl.reset_live()
        ctl.set_band_on(0, False)                                       # flat: inaudible
        ctl.set_band_on(0, True)
        ctl.set_band_on(5, False)
        assert ctl.undo_left() == pytest.approx(300.0)
        assert ctl.undo_reset() is True and ctl_mod._live_dict(ctl.state) == want
        assert ctl.state.live_highshelf.on is True                      # as it was before the reset
        # a reset of a live EQ with a band OUT brings every band back IN, and the undo puts the
        # OUT back
        ctl.set_band_on(1, False)
        want = ctl_mod._live_dict(ctl.state)
        assert ctl_mod._audible_live(ctl.state)[1] == (0.0,)
        ctl.reset_live()
        assert ctl.state.live_peq[1].on is True and ctl.undo_left() > 0
        assert ctl.undo_reset() is True and ctl.state.live_peq[1].on is False
        # an audible switch (a stored cut going IN) after a reset ends the undo, said so
        ctl.state.live_peq[1].on = True
        ctl.reset_live()
        ctl.set_value("live_peq.1.gain", -4.0)                          # an audible edit...
        assert ctl.undo_left() == 0.0
        ctl.reset_live()
        ctl.state.live_peq[2].gain = -3.0                               # (a stored cut, OUT)
        ctl.state.live_peq[2].on = False
        ctl._undo["after"] = ctl_mod._audible_live(ctl.state)          # as if it was so at reset
        assert ctl.undo_left() > 0
        ctl.set_band_on(2, True)                                        # ...and a band going IN
        assert ctl.undo_left() == 0.0
        assert ctl.focus.hint_now() == "P3 IN"                          # the press's answer wins
        await ctl.stop()

    run(body())


# ======================================================================= D-016
M4 = load_profile("m4", {}, probe=False)


def make_m4(tmp_path: Path, state: State | None = None, **kw: Any) -> tuple[Controller, FakeClient]:
    """`make` on the m4 profile (IN1/IN2 spare: AUX IN can route them), engine already playing."""
    st = state.copy() if state is not None else State()
    st.profile = "m4"
    client = FakeClient(dc.generate(M4, st))
    lv = dc.fader_levels(st)
    client.faders[0] = {"volume": lv[0], "mute": st.muted}
    client.faders[MARKER_FADER]["volume"] = MARKER_DB
    opts: dict[str, Any] = dict(sleep=fast_sleep, poll_s=3600, meter_s=3600,
                                persist_debounce_s=0.05, persist_max_delay_s=0.5,
                                device_probe=lambda d: True)
    opts.update(kw)
    ctl = Controller(client, M4, st, Paths(tmp_path / "state.json", tmp_path / "active.yml"), **opts)
    return ctl, client


def test_structure_fields_are_every_rebuild_field_but_the_profile() -> None:
    from k1000.state import REBUILD_FIELDS
    assert set(ctl_mod.STRUCTURE_FIELDS) == set(REBUILD_FIELDS) - {"profile"}
    assert {"aux_mode", "loud_on", "deharsh_on", "mono_subs", "dev_audition"} == \
        set(ctl_mod.STRUCTURE_FIELDS)


def test_d016_pills_follow_the_state_and_what_the_profile_routes(tmp_path: Path) -> None:
    """AUX MIC / LINE after POL INV, only where an input is spare (m4); LOUDNESS and DE-HARSH
    after MONO SUB. All off (the default) = no pill."""
    st = State(sub_inverted=True, mono_subs=True, aux_mode="mic", loud_on=True, deharsh_on=True)
    m4, _ = make_m4(tmp_path / "m4", st)
    assert m4.pills() == ["POL INV SUB", "AUX MIC", "MONO SUB", "LOUDNESS", "DE-HARSH"]
    m4.state.aux_mode = "line"
    assert m4.pills()[1] == "AUX LINE"
    assert m4.setup_readonly() == {"dev_audition": ctl_mod.HINT_AUDITION_DEV_ONLY}
    assert m4.aux_available is True
    dev, _ = make(tmp_path / "dev", st)
    assert dev.pills() == ["POL INV SUB", "MONO SUB", "LOUDNESS", "DE-HARSH"]     # nothing routed
    assert dev.aux_available is False
    assert make(tmp_path / "off", State())[0].pills() == []


@pytest.mark.parametrize("field,value,said", [
    ("deharsh_on", True, "DE-HARSH ON"), ("loud_on", True, "LOUDNESS ON"),
    ("aux_mode", "mic", "AUX IN 1: MIC"), ("aux_mode", "line", "AUX IN 1/2: LINE"),
])
def test_d016_switch_is_a_rebuild_under_main_mute_and_said(tmp_path: Path, field: str, value: Any,
                                                         said: str) -> None:
    """SETUP's D-016 switches go the way of Mono: refused unconfirmed while playing, then (chosen
    + push) one whole-config upload with Main muted around it; the engine gets the block, the
    screen says it, state.json and active.yml keep it. Switched off again: the config is
    f2671b0's again."""
    async def body() -> None:
        ctl, cl = make_m4(tmp_path, State(master_db=-10.0))
        await boot(ctl)
        before = copy.deepcopy(cl.cfg)
        assert not ctl.toggle(field)                                 # unconfirmed, playing
        assert ctl.focus.hint == ctl_mod.HINT_CONFIRM_SETUP
        n = len(cl.log)
        assert ctl.set_choice(field, value) is True
        assert ctl.focus.hint_now() == said
        assert await ctl.settle(5.0)
        ops = nomark(cl.log[n:])
        assert [e[0] for e in ops] == ["mute", "config", "mute"]
        cfg = ops[1][1]
        if field == "deharsh_on":
            assert set(cfg["processors"]) == {dc.DH_COMP} and dc.DH_SPLIT in cfg["mixers"]
        elif field == "loud_on":
            assert dc.SUB_LOUDNESS in cfg["filters"]
        else:
            assert dc.AUX_MIX in cfg["mixers"] and cfg["mixers"]["input"]["channels"]["out"] == 4
        assert getattr(ctl._applied, field) == value
        assert ctl.persist_state().to_dict()[field] == value
        await until(lambda: (tmp_path / "state.json").exists(), what="saved")
        await ctl.settle(2.0)
        # the 1 Hz poll finds nothing to fix in the new structure
        n = len(cl.log)
        await ctl.poll_once()
        assert nomark(cl.log[n:]) == [] and ctl.status.drift == []
        # back off: the f2671b0 config again
        assert ctl.set_choice(field, "off" if field == "aux_mode" else False) is True
        assert await ctl.settle(5.0)
        assert cl.cfg == before
        await ctl.stop()
        saved = json.loads((tmp_path / "state.json").read_text())
        assert saved[field] == ("off" if field == "aux_mode" else False)

    run(body())


def test_d016_unapplied_switch_wins_over_adoption_and_is_not_persisted(tmp_path: Path) -> None:
    """A D-016 switch set while the engine is unreachable: adoption keeps it (it is not read
    from the structure), state.json keeps what RUNS (`persist_state`), the next sync applies it;
    a failed upload reverts it, said."""
    async def body() -> None:
        ctl, cl = make_m4(tmp_path, State(master_db=-10.0))
        await boot(ctl)

        async def refuse(text: str, timeout: float = 10.0) -> None:
            raise CdspError("SetConfig", "ConfigValidationError", "fake")

        real = cl.set_config_yaml
        cl.set_config_yaml = refuse  # type: ignore[method-assign]
        ctl.set_choice("loud_on", True)
        assert await ctl.settle(5.0)
        assert ctl.state.loud_on is False                              # reverted
        cl.set_config_yaml = real  # type: ignore[method-assign]
        # unreachable: the edit waits, adoption keeps it, the disk keeps what runs
        await cl.drop()
        ctl.set_mute(True)
        ctl.state.deharsh_on = True
        ctl._local_edit()
        assert ctl.persist_state().deharsh_on is False
        await cl.reconnect()
        await until(lambda: ctl.engine_ready, what="resync")
        assert await ctl.settle(5.0)
        assert ctl.state.deharsh_on is True and dc.DH_COMP in (cl.cfg.get("processors") or {})
        assert ctl.persist_state().deharsh_on is True
        await ctl.stop()

    run(body())


def test_d016_adopt_keeps_a_switch_not_applied_yet(tmp_path: Path) -> None:
    """`_adopt` never takes the D-016 switches from the running structure: a switch set locally
    and not uploaded yet stays set, and the engine view keeps what runs."""
    async def body() -> None:
        ctl, cl = make_m4(tmp_path, State(master_db=-10.0))
        await boot(ctl)
        ctl.state.aux_mode = "line"                       # as if just set, not applied
        view = await ctl._adopt(copy.deepcopy(cl.cfg), copy.deepcopy(cl.faders), connect=False)
        assert ctl.state.aux_mode == "line" and view.aux_mode == "off"
        assert ctl.persist_state().aux_mode == "off"
        await ctl.stop()

    run(body())


def test_d016_cuts_made_outside_k1000_are_kept_raises_pulled_down(tmp_path: Path) -> None:
    """CamillaGUI on the D-016 blocks, K1000's safe-direction rule as for a trim: the loudness
    reference set below its range (less boost), its mid attenuation switched on, the de-harsh
    ratio raised to 10:1, a threshold below the range: all KEPT and shown, never pushed back up.
    A raise (a treble boost K1000 holds at 0 dB, a 2:1 ratio) is pulled back, the boost in 0.1 dB
    steps. Every value here is one the real engine accepts (probed: a negative boost is refused)."""
    async def body() -> None:
        ctl, cl = make_m4(tmp_path, State(master_db=-20.0, loud_on=True, deharsh_on=True))
        await boot(ctl)
        loud = cl.cfg["filters"][dc.SUB_LOUDNESS]["parameters"]
        comp = cl.cfg["processors"][dc.DH_COMP]["parameters"]
        loud["reference_level"], comp["threshold"] = -50.0, -60.0
        for _ in range(2):
            n = len(cl.log)
            await ctl.poll_once()
            assert nomark(cl.log[n:]) == []
        assert sorted(ctl.status.drift) == ["dh_comp THRESH -60.0 dB", "sub_loudness REF -50.0 dB"]
        assert ctl.state.deharsh_thresh_db == -48.0 and ctl.state.loud_ref_db == -40.0  # clamped
        loud["reference_level"], comp["threshold"] = -10.0, -48.0      # back in range: adopted
        loud["attenuate_mid"], comp["factor"] = True, 10.0
        n = len(cl.log)
        await ctl.poll_once()
        assert nomark(cl.log[n:]) == []
        assert sorted(ctl.status.drift) == ["dh_comp RATIO 10", "sub_loudness MID CUT"]
        assert loud["attenuate_mid"] is True and comp["factor"] == 10.0
        # raises are pulled back down
        loud["high_boost"], loud["attenuate_mid"] = 3.0, False
        comp["factor"] = 2.0
        n = len(cl.log)
        await ctl.poll_once()
        hb = sets_for(cl.log[n:], "/filters/sub_loudness/parameters/high_boost")
        assert hb[-1] == 0.0 and len(hb) >= 30
        assert all(abs(b - a) <= 0.1 + 1e-9 for a, b in zip([3.0] + hb, hb))
        assert sets_for(cl.log[n:], "/processors/dh_comp/parameters/factor")[-1] == 3.0
        assert ctl.status.drift == []
        await ctl.stop()

    run(body())


@pytest.mark.parametrize("aux", ["mic", "line", "off"])
def test_d016_torn_state_json_keeps_the_switches_active_yml_runs(tmp_path: Path, aux: str) -> None:
    """recover_state after a torn state.json: active.yml IS the tune, D-016 switches included
    (read from its blocks, like mono from the split mixer). Before (D-016 review), the rebuilt
    state had them off: k1000 then rewrote active.yml without the blocks and restored the playing
    engine, Main muted around the upload (a dip mid-show), de-harsh, loudness and AUX gone."""
    tuned = State(profile="m4", master_db=-10.0, mono_subs=True, deharsh_on=True, loud_on=True,
                  aux_mode=aux, deharsh_thresh_db=-30.0, loud_boost_db=8.0, aux_gain_db=-6.0)
    active_text = dc.to_yaml(dc.generate(M4, tuned))
    (tmp_path / "active.yml").write_text(active_text)
    (tmp_path / "state.json").write_text('{"schema": 1, "deharsh_on": true, "sys_peq": [{"fr')
    st, note = recover_state(Paths(tmp_path / "state.json", tmp_path / "active.yml"))
    assert note == ctl_mod.NOTE_REBUILT and st.profile == "m4" and st.muted is True
    assert (st.mono_subs, st.deharsh_on, st.loud_on, st.aux_mode) == (True, True, True, aux)
    # the AUX level is in the config only while AUX IN is on (an owned value of its block)
    assert (st.deharsh_thresh_db, st.loud_boost_db, st.aux_gain_db) == (
        -30.0, 8.0, -6.0 if aux != "off" else State().aux_gain_db)
    assert dc.to_yaml(dc.generate(M4, st)) == active_text           # the very same config
    # dev (no spare input: the AUX filters exist, unrouted) reads the same way
    dev_text = dc.to_yaml(dc.generate(DEV, State(aux_mode=aux, loud_on=True)))
    assert dc.feature_switches(yaml.safe_load(dev_text)) == {
        "aux_mode": aux, "loud_on": True, "deharsh_on": False}
    assert dc.feature_switches({"filters": {}}) == {}               # not a K1000 config

    async def body() -> None:
        live = State.from_dict({**st.to_dict(), "muted": False})
        ctl, cl = make_m4(tmp_path, live)                           # the engine plays it
        ctl.state.muted = ctl._applied.muted = True                 # what recover_state hands over
        await boot(ctl)
        assert not any(e[0] == "config" for e in cl.log), "the playing engine was restored"
        assert not any(e[0] == "mute" and e[1] == 0 for e in cl.log)
        assert yaml.safe_load((tmp_path / "active.yml").read_text()) == yaml.safe_load(active_text)
        await ctl.stop()
        assert json.loads((tmp_path / "state.json").read_text())["deharsh_on"] is True

    run(body())


def test_d016_loudness_boost_detents_reach_an_engine_that_keeps_it_as_f32(tmp_path: Path) -> None:
    """The engine keeps the Loudness boost as f32 and its read-back is widened (FakeClient.set_value
    confirms the way CamillaClient does). Each knob detent goes out as single 0.1 dB SetValue
    steps (a trim, `_TRIM_POINTERS`: 6.1, 6.2...), and every one must be confirmed: before the
    tolerant `cdsp._matches` (D-016 review), the first step of each detent failed as NotApplied,
    said ENGINE REFUSED THE CHANGE and resynced, so three detents left the boost at 6.3."""
    async def body() -> None:
        ctl, cl = make_m4(tmp_path, State(master_db=-20.0, loud_on=True))
        await boot(ctl)
        ptr = "/filters/sub_loudness/parameters/low_boost"
        n = len(cl.log)
        for _ in range(3):
            ctl.nudge("loud_boost_db", 1)
            assert await ctl.settle(5.0)
        assert ctl.state.loud_boost_db == 7.5
        assert cl.cfg["filters"][dc.SUB_LOUDNESS]["parameters"]["low_boost"] == 7.5
        steps = sets_for(cl.log[n:], ptr)
        assert 6.1 in steps and steps[-1] == 7.5
        assert ctl.focus.hint_now() != "ENGINE REFUSED THE CHANGE"
        await ctl.stop()

    run(body())


def test_d016_a_mic_muted_on_aux_mix_from_the_gui_stays_muted(tmp_path: Path) -> None:
    """CamillaGUI mutes the AUX mic on `aux_mix` (where it shows going into L/R) and cuts IN1 in
    the input mixer. Before (D-016 review) any mixer difference was a rebuild and the restore put
    the mic back at -3 dB under a Main mute. Now: held and shown, no upload; a restore for
    another reason (a pipeline step dropped) keeps them; and the kept cuts never restore again."""
    async def body() -> None:
        ctl, cl = make_m4(tmp_path, State(master_db=-20.0, aux_mode="mic"))
        await boot(ctl)
        amix = cl.cfg["mixers"][dc.AUX_MIX]["mapping"]
        for e in amix:
            for x in e["sources"]:
                if x["channel"] == 2:
                    x["mute"] = True                                 # the mic, both sides
        cl.cfg["mixers"]["input"]["mapping"][2]["sources"][0]["gain"] = -30.0
        for _ in range(3):
            n = len(cl.log)
            await ctl.poll_once()
            assert nomark(cl.log[n:]) == []                          # no restore, no mute
        assert sorted(ctl.status.drift) == ["aux_mix 2->0 MUTED", "aux_mix 2->1 MUTED",
                                            "input 0->2 -30.0 dB"]
        assert "CUTS/MUTES" in ctl.status.message
        # the GUI also drops a pipeline step: restored after the second look, the cuts kept
        cl.cfg["pipeline"] = cl.cfg["pipeline"][:-2] + cl.cfg["pipeline"][-1:]
        n = len(cl.log)
        await ctl.poll_once()
        await ctl.poll_once()
        ops = nomark(cl.log[n:])
        assert [e[0] for e in ops][:3] == ["mute", "config", "mute"]
        assert cl.cfg["pipeline"] == dc.generate(M4, ctl.state)["pipeline"]
        assert all(x.get("mute") is True for e in cl.cfg["mixers"][dc.AUX_MIX]["mapping"]
                   for x in e["sources"] if x["channel"] == 2)
        assert cl.cfg["mixers"]["input"]["mapping"][2]["sources"][0]["gain"] == -30.0
        ctl._last_restore = -math.inf                                # the interval is no shield
        for _ in range(2):
            n = len(cl.log)
            await ctl.poll_once()
            assert nomark(cl.log[n:]) == []                          # held, not restored again
        assert "aux_mix 2->0 MUTED" in ctl.status.drift
        await ctl.stop()

    run(body())


def test_d016_keep_cuts_carries_an_aux_mix_mute_into_a_restore() -> None:
    """The review's case: `_keep_cuts(with_live_devices(generate(...), live), live)` with the
    aux_mix mic sources muted, or at -30 dB, uploads them muted / at -30 dB, not at -3 dB."""
    st = State(profile="m4", aux_mode="mic")
    want = dc.generate(M4, st)
    for cut in ({"mute": True}, {"gain": -30.0}):
        live = copy.deepcopy(want)
        for e in live["mixers"][dc.AUX_MIX]["mapping"]:
            for x in e["sources"]:
                if x["channel"] == 2:
                    x.update(cut)
        out = ctl_mod._keep_cuts(dc.with_live_devices(want, live), live)
        srcs = [x for e in out["mixers"][dc.AUX_MIX]["mapping"] for x in e["sources"]
                if x["channel"] == 2]
        assert len(srcs) == 2 and all(x.get("mute", False) is cut.get("mute", False)
                                      and x["gain"] == cut.get("gain", -3.0) for x in srcs)
    assert want["mixers"][dc.AUX_MIX]["mapping"][0]["sources"][1] == {
        "channel": 2, "gain": -3.0, "inverted": False, "scale": "dB"}   # not mutated


def test_d016_keep_cuts_carries_quieter_block_values_into_a_restore() -> None:
    st = State(loud_on=True, deharsh_on=True)
    want = dc.generate(M4, st)
    live = copy.deepcopy(want)
    lp = live["filters"][dc.SUB_LOUDNESS]["parameters"]
    cp = live["processors"][dc.DH_COMP]["parameters"]
    lp.update(low_boost=2.0, reference_level=-30.0, attenuate_mid=True, high_boost=1.0)
    cp.update(threshold=-40.0, factor=6.0, makeup_gain=-2.0)
    out = ctl_mod._keep_cuts(want, live)
    olp = out["filters"][dc.SUB_LOUDNESS]["parameters"]
    ocp = out["processors"][dc.DH_COMP]["parameters"]
    assert (olp["low_boost"], olp["reference_level"], olp["attenuate_mid"]) == (2.0, -30.0, True)
    assert olp["high_boost"] == 0.0                                    # a raise: not carried
    assert (ocp["threshold"], ocp["factor"], ocp["makeup_gain"]) == (-40.0, 6.0, -2.0)
    assert want["processors"][dc.DH_COMP]["parameters"]["factor"] == 3.0   # not mutated
    assert ctl_mod._keep_cuts(want, {"filters": None}) == want


@pytest.mark.engine
@needs_engine
def test_engine_d016_loudness_boost_detents_and_a_mute_right_after_one(tmp_path: Path) -> None:
    """On the Pi's engine (D-016 review): it keeps the Loudness boost as f32 and reads it back
    widened (6.1 -> 6.099999904632568). Knob detents of the boost reach the engine (before: each
    one ENGINE REFUSED, resynced at +0.1 dB), a treble boost raised from CamillaGUI is pulled
    back to 0 dB with no CONFIG DRIFT left (before: given up at 0.7 dB), and a B4 mute pressed
    50 ms after a detent reaches the engine at once (before: 0.56 s behind the failing confirm)."""
    async def body() -> None:
        paths = Paths(tmp_path / "state.json", tmp_path / "active.yml")
        st = State(profile="m4", master_db=-20.0, loud_on=True)
        client = CamillaClient(f"ws://127.0.0.1:{ENGINE_PORT}", backoff_min=0.05, backoff_max=0.3,
                               fader_max_db=ctl_mod.FADER_CEILING_DB)
        ctl = Controller(client, M4, st, paths, transform=_test_transform, device_probe=None,
                         poll_s=0.2, meter_s=0.1, persist_debounce_s=0.2, boot_ramp_s=1.0)
        ctl.save_now()                                   # the engine unit waits for this file
        eng = Engine(paths.active_config, tmp_path / "camilladsp.log")
        await asyncio.to_thread(eng.start)
        obs = CamillaClient(eng.url, backoff_min=0.05, backoff_max=0.2)
        ptr = "/filters/sub_loudness/parameters/"
        try:
            await obs.connect(timeout=3.0)
            await ctl.start()
            await until(lambda: ctl.engine_ready and not ctl.ramping, timeout=10.0, what="boot ramp")
            for want in (6.5, 7.0, 7.5):
                ctl.nudge("loud_boost_db", 1)
                assert await ctl.settle(5.0)
                assert ctl.state.loud_boost_db == want
                assert ctl.focus.hint_now() != "ENGINE REFUSED THE CHANGE"
            assert await obs.get_value(ptr + "low_boost") == pytest.approx(7.5, abs=1e-6)

            await obs.set_value(ptr + "high_boost", 1.0)            # CamillaGUI raises the treble

            async def treble_back() -> bool:
                return abs(await obs.get_value(ptr + "high_boost")) < 1e-6
            await until(treble_back, timeout=10.0, what="treble boost pulled back")
            await asyncio.sleep(0.6)
            assert ctl.status.drift == [] and "DRIFT" not in (ctl.status.message or "")

            ctl.nudge("loud_boost_db", 1)
            await asyncio.sleep(0.05)
            t0 = time.monotonic()
            ctl.set_mute(True)

            async def muted() -> bool:
                return (await obs.get_faders())[0]["mute"] is True
            await until(muted, timeout=3.0, what="B4 mute on the engine")
            assert time.monotonic() - t0 < 0.3
            assert await ctl.settle(5.0)
            assert ctl.state.loud_boost_db == 8.0
            assert await obs.get_value(ptr + "low_boost") == pytest.approx(8.0, abs=1e-6)
        finally:
            await ctl.stop()
            await client.close()
            await obs.close()
            await asyncio.to_thread(eng.kill)

    run(body(), limit=90.0)
