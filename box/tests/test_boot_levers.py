# SPDX-License-Identifier: GPL-3.0-or-later
"""The userspace boot levers (#t-021): the engine first, then the heavy surfaces."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from k1000 import __main__ as main_mod
from k1000 import dspconfig as dc
from k1000.control import BOOT_STEP_MAX_DB, EngineSettings
from k1000.state import State


def _hold(ctl, stop, hold_s, poll_s=0.01):
    async def body():
        t0 = time.monotonic()
        ok = await main_mod.hold_for_engine(ctl, stop, hold_s=hold_s, poll_s=poll_s)
        return ok, time.monotonic() - t0
    return asyncio.run(body())


def test_hold_returns_when_the_first_sync_is_done():
    ctl = SimpleNamespace(engine_ready=False)
    stop = asyncio.Event()

    async def body():
        async def ready():
            await asyncio.sleep(0.05)
            ctl.engine_ready = True
        t = asyncio.get_running_loop().create_task(ready())
        t0 = time.monotonic()
        ok = await main_mod.hold_for_engine(ctl, stop, hold_s=2.0, poll_s=0.01)
        await t
        return ok, time.monotonic() - t0
    ok, dt = asyncio.run(body())
    assert ok and dt < 0.5


def test_hold_is_bounded_without_an_engine():
    ok, dt = _hold(SimpleNamespace(engine_ready=False), asyncio.Event(), hold_s=0.1)
    assert not ok and 0.09 <= dt < 0.5


def test_hold_never_outlasts_a_stop():
    stop = asyncio.Event()
    stop.set()
    ok, dt = _hold(SimpleNamespace(engine_ready=False), stop, hold_s=5.0)
    assert not ok and dt < 0.5


def test_hold_is_free_when_already_synced():
    ok, dt = _hold(SimpleNamespace(engine_ready=True), asyncio.Event(), hold_s=5.0)
    assert ok and dt < 0.1


def test_engine_link_retries_fast_and_surfaces_hold_briefly():
    assert main_mod.ENGINE_RETRY_MAX_S <= 0.25
    assert 0 < main_mod.SURFACES_HOLD_S <= 3.0


def test_boot_ramp_default_is_1_5_s_and_the_step_cap_holds():
    assert dc.BOOT_RAMP_S == 1.5 and EngineSettings().boot_ramp_s == 1.5
    assert BOOT_STEP_MAX_DB == 6.0
    # -40 dB -> 0 dB over the new default: Main climbs in steps no larger than the cap.
    plan = dc.boot_ramp_plan(State(master_db=0.0, muted=False), dc.BOOT_GAIN_DB, dc.BOOT_RAMP_S)
    main = [op.db for _, ops in plan for op in ops
            if isinstance(op, dc.Fader) and op.index == dc.FADER_MAIN]
    assert main[0] == dc.BOOT_GAIN_DB and main[-1] == 0.0
    assert max(b - a for a, b in zip(main, main[1:])) <= BOOT_STEP_MAX_DB + 1e-9
