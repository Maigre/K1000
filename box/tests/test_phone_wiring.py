# SPDX-License-Identifier: GPL-3.0-or-later
"""The phone's wiring into the existing control path (WIRING.md §1-4, D-007): the box's mute
button (B4 since D-013, B2 before) and a phone acting on the same mute, the PHONE page reached
from SETUP, its knobs, its rendering.

These run once WIRING.md is applied (Controller.mute_view / SETUP > Phone exist) and skip before,
with that reason: the phone module itself (tests/test_web.py) does not depend on them.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from k1000.control import Controller
from k1000.ui import input_map as im
from k1000.web.server import PhoneBridge, PhoneScreenInfo

from test_web import boot, make_ctl, run, until

WIRED = hasattr(Controller, "mute_view") and "PHONE" in im.ALL_PAGES
pytestmark = pytest.mark.skipif(not WIRED, reason="WIRING.md not applied yet (Controller.mute_view, "
                                                  "SETUP > Phone)")


def _sw(idx: int, kind: str) -> Any:
    from k1000.hw.mcp23017 import SwitchEvent
    return SwitchEvent(idx, kind, 0.0)


def _enc(idx: int, delta: int) -> Any:
    from k1000.hw.mcp23017 import EncoderEvent
    return EncoderEvent(idx, delta, 0.0)


def _mute_button(ctl: Controller) -> int:
    """The physical switch that is the MUTE button (B4, D-013) in this Controller's control map."""
    return next(i for i in range(8) if ctl.controls.switch_role(i) == ("B", 3))


def _push(ctl: Controller, n: int) -> int:
    return next(i for i in range(8) if ctl.controls.switch_role(i) == ("P", n))


@pytest.mark.parametrize("page", ["MAIN", "HWTEST"])
def test_b4_mute_acts_on_what_its_press_saw(tmp_path: Path, page: str) -> None:
    """psafe-2: B4 (mute) 'long' fires 0.8 s after the press, finger still down. A phone muting during
    those 0.8 s must not turn the operator's MUTE into an UNMUTE; a phone muting during an
    unmute press makes that unmute be refused."""
    async def go() -> None:
        ctl, eng = make_ctl(tmp_path)
        await boot(ctl)
        ctl.goto_page(page)
        b4, phone, phone2 = _mute_button(ctl), PhoneBridge(ctl), PhoneBridge(ctl)
        assert b4 == 7                                       # sw7 under the default controls map
        main_muted = lambda: eng.faders[0]["mute"] is True   # noqa: E731
        # plain B4: press + long mutes, press + long unmutes (as before)
        ctl.handle_event(_sw(b4, "press")); ctl.handle_event(_sw(b4, "long"))
        await until(main_muted, what="B4 muted")
        ctl.handle_event(_sw(b4, "release"))
        ctl.handle_event(_sw(b4, "press")); ctl.handle_event(_sw(b4, "long"))
        await until(lambda: eng.faders[0]["mute"] is False, what="B4 unmuted")
        ctl.handle_event(_sw(b4, "release"))
        # the race: B4 pressed on an UNMUTED box, a phone mutes, B4 long -> still MUTED
        ctl.handle_event(_sw(b4, "press"))
        assert phone.apply({"t": "mute", "on": True})[0]
        await until(main_muted, what="phone muted")
        ctl.handle_event(_sw(b4, "long"))
        await asyncio.sleep(0.05)
        assert ctl.displayed_muted() is True and main_muted() and ctl.state.muted is True
        ctl.handle_event(_sw(b4, "release"))
        # B4 pressed on a MUTED box (to unmute); phone 1 unmutes, phone 2 mutes again; B4 long
        # must not undo phone 2's mute
        ctl.handle_event(_sw(b4, "press"))
        _, g = phone.mute_view()
        assert phone.apply({"t": "mute", "on": False, "seq": g})[0]
        await until(lambda: eng.faders[0]["mute"] is False, what="phone unmuted")
        assert phone2.apply({"t": "mute", "on": True})[0]
        await until(main_muted, what="phone 2 muted")
        ctl.handle_event(_sw(b4, "long"))
        await asyncio.sleep(0.05)
        assert main_muted() and ctl.state.muted is True
        if page != "HWTEST":   # (HWTEST shows its own event log instead of hints)
            assert ctl.focus.hint == "MUTE CHANGED · LOOK, HOLD B4 AGAIN"
        ctl.handle_event(_sw(b4, "release"))
        # a fresh look: B4 unmutes again
        ctl.handle_event(_sw(b4, "press")); ctl.handle_event(_sw(b4, "long"))
        await until(lambda: eng.faders[0]["mute"] is False, what="B4 unmuted again")
        await ctl.stop()
    run(go())


def test_phone_unmute_is_refused_after_b4_muted(tmp_path: Path) -> None:
    """psafe-1 with the Controller's generation: a phone's unmute based on a view older than a
    B4 mute (even a repeated one) is refused."""
    async def go() -> None:
        ctl, eng = make_ctl(tmp_path)
        await boot(ctl)
        b4, phone = _mute_button(ctl), PhoneBridge(ctl)
        ctl.set_mute(True)
        await until(lambda: eng.faders[0]["mute"] is True)
        _, g = phone.mute_view()
        ctl.handle_event(_sw(b4, "press")); ctl.handle_event(_sw(b4, "long"))   # B4: unmute
        ctl.handle_event(_sw(b4, "release"))
        ctl.set_mute(True)                                                     # and mute again
        await until(lambda: eng.faders[0]["mute"] is True)
        ok, v, why = phone.apply({"t": "mute", "on": False, "seq": g})
        assert (ok, v) == (False, True) and "MUTE CHANGED" in why
        assert ctl.set_mute(False, seen=g) is True        # the same rule, from the controller side
        _, g2 = phone.mute_view()
        assert phone.apply({"t": "mute", "on": False, "seq": g2})[0]
        await until(lambda: eng.faders[0]["mute"] is False)
        await ctl.stop()
    run(go())


class _FakePhone:
    def __init__(self) -> None:
        self.on, self.qr = True, []

    def toggle(self) -> bool:
        self.on = not self.on
        return self.on

    def select_qr(self, delta: int = 1) -> str:
        self.qr.append(delta)
        return "url"

    def screen_info(self) -> PhoneScreenInfo:
        return PhoneScreenInfo(psk="m7kq-4tzh-p2xv", ap_up=True, on=self.on)


def test_setup_phone_page_and_its_knobs(tmp_path: Path) -> None:
    async def go() -> None:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        ctl.goto_page("SETUP")
        idx = [i.field for i in im.SETUP_ITEMS].index("PHONE")
        assert im.SETUP_ITEMS[-1].field == "HWTEST"           # Knob test stays last
        ctl.focus.item = idx
        e1 = _push(ctl, 0)
        ctl.handle_event(_sw(e1, "press")); ctl.handle_event(_sw(e1, "short"))
        assert ctl.page == "PHONE"
        assert ctl.phone_toggle() is None and "PHONE DISABLED" in (ctl.focus.hint or "")   # no surface
        ctl.phone = _FakePhone()
        ctl.handle_event(_sw(e1, "press")); ctl.handle_event(_sw(e1, "short"))
        assert ctl.phone.on is False and ctl.focus.hint == "PHONE OFF · KNOBS ONLY"
        ctl.handle_event(_sw(e1, "press")); ctl.handle_event(_sw(e1, "short"))
        assert ctl.phone.on is True and ctl.focus.hint == "PHONE CONTROL ON"
        enc = next(i for i in range(4) if ctl.controls.encoder_role(i) == 0)
        ctl.handle_event(_enc(enc, 1)); ctl.handle_event(_enc(enc, -2))
        assert ctl.phone.qr == [1, -1]
        # the page renders; B1 leaves (D-013): its press does nothing, a tap goes back to SETUP
        # (where PHONE was opened), a hold goes home
        from k1000.ui import pages
        img = pages.render("PHONE", ctl.state, ctl.status, ctl.focus, phone=ctl.phone.screen_info())
        assert img.size == (480, 320)
        b1 = next(i for i in range(8) if ctl.controls.switch_role(i) == ("B", 0))
        ctl.handle_event(_sw(b1, "press"))
        assert ctl.page == "PHONE"
        ctl.handle_event(_sw(b1, "short"))
        assert ctl.page == "SETUP" and im.SETUP_ITEMS[ctl.focus.item].field == "PHONE"
        ctl.handle_event(_sw(e1, "press")); ctl.handle_event(_sw(e1, "short"))
        assert ctl.page == "PHONE"
        ctl.handle_event(_sw(b1, "press")); ctl.handle_event(_sw(b1, "long"))
        assert ctl.page == "SHOW"
        await ctl.stop()
    run(go())
