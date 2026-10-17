# SPDX-License-Identifier: GPL-3.0-or-later
"""Knob and button semantics per page: hardware events -> UI actions (UI v1, D-009).

Why a pure table: the physical position of every knob and button on the Zynthian V4 shell is
not confirmed yet, and the show-time meaning must be testable without hardware. So:

    physical event --(cfg 'controls' mapping)--> logical E1..E4 / B1..B4 / push P1..P4
                   --(KNOBS table, per page)----> Action objects for the controller

`map_event()` never touches audio. It only keeps the *input bookkeeping* of `Focus` (which
encoder pushes are held, when each knob last turned, a pending SETUP confirm, the ALIGN unlock,
the HWTEST log) and returns actions. The dispatcher then runs, for each action,
`apply_focus(focus, action)` (UI-local: band/item selection, hints, the edit highlight, the
ALIGN lock) and, when that returns False, hands the action to the Controller.

Pages (D-009, buttons D-013): SHOW (front) <-> ALIGN on a B1 tap; SETUP on a B1 hold; PHONE and
HWTEST reached from SETUP.
- Buttons (D-013): B1 PAGE · B2 BAND/UNLOCK · B3 NOTCH · B4 MUTE.
- SHOW: E1 MASTER, E2/E3/E4 = FREQ/Q/GAIN of the selected live band (D-015: GAIN on E4, Q or
  the shelf's SLOPE on E3; GAIN was E3 until then). B2 tap = next band in
  LS 1 2 3 4 HS order (wraps). B3 tap = the notch helper (Controller.notch_helper); B3 hold =
  the selected band IN/OUT (D-014, Controller.toggle_band). An encoder push is FINE while held,
  never a select.
- ALIGN: E1 MASTER and E3 BALANCE live; E2 DELAY (the signed sub/top alignment, D-011) and E4
  LO-CUT, the two bottom knobs, are LOCKED (D-015: DELAY on E2, BALANCE on E3; the other way
  round until then): a turn shows HINT_LOCKED and changes nothing. A B2 hold unlocks them for
  UNLOCK_S, renewed by every input on ALIGN (so any UNLOCK_S of idle relocks); a B2 tap relocks
  at once; leaving ALIGN relocks.
- SETUP (D-013 groups, D-017 knobs): the items grouped by speaker floor (TOPS, SUBS, INPUT,
  LIVE EQ, PHONE, BOX). E1 selects the item (drawn on a fixed screen row), E2 jumps a group, E3
  is the value: a number moves as E3 turns, an everyday ON/off goes ON on a right turn and off on
  a left one, applied at once. A RISKY row (SETUP_RISKY: every pipeline rebuild, both polarities,
  Lo-Cut) only PROPOSES on an E3 turn (a popup, `now -> proposed`) and an E3 push applies it: two
  gestures, the value always visible; an E1 or E2 turn, leaving the page or PICK_WINDOW_S
  cancels. Pages open and actions run on an E3 push; Reset live EQ and opening Laptop access
  need a second E3 push within CONFIRM_WINDOW_S (the same popup). E1 and E2 pushes only point to
  E3. A row the Controller holds read-only (`Focus.readonly`), a number included, only shows its
  reason.
- D-016 (2026-10-16, all OFF by default): TOPS De-harsh (ON/off, a rebuild) + its freq and
  threshold, SUBS Loudness (ON/off, a rebuild) + its boost and reference, INPUT AUX IN 1/2
  (off / MIC / LINE, a rebuild) + its level. The switches are risky rows like Mono; the numbers
  turn like a trim.
- B1: tap (on release) = SHOW <-> ALIGN, and back from SETUP to SHOW, from PHONE to SETUP;
  hold = SETUP from SHOW/ALIGN, SHOW from SETUP/PHONE. The Controller brings ALIGN and SETUP
  back to SHOW after HOME_AFTER_S without input.

Safety rules carried here:
- B4 mutes/unmutes on a LONG press only, on EVERY page, the Knob test included; a short press
  only shows a hint. B4 emits an explicit `Mute(bool)` when the dispatcher passes the mute the
  screen shows (`map_event(..., muted=...)`): the operator presses for the opposite of what
  they see, never for a state the controller has not adopted yet.
- Resetting the live EQ is a SETUP item with a confirm (and a one-step undo in the Controller):
  a B3 tap on SHOW only ever selects a band and sets an inaudible one up (gain is never touched).
  A B3 hold (0.8 s) on SHOW switches ONE band, the selected one, IN or OUT (D-014): OUT keeps
  its settings, so a second hold brings back exactly what was there. ONLY once the button map
  is confirmed (`ControlMap.buttons_confirmed`): until the Knob test proved which button is B3,
  a hold on it may be an emergency mute aimed at B4, and taking a band (often the notch that
  holds the ring) OUT then makes things worse. Unconfirmed, the hold only says
  HINT_B3_HOLD_UNCONFIRMED, as the B3 hold did before D-014.
- DELAY (every settled change dips the subs or the tops under the delay guard) keeps v0's
  wake-up detent on top of the lock: the first detent after ARM_WINDOW_S at rest only wakes it.
- Every risky SETUP row (SETUP_RISKY, and every pipeline rebuild whatever that list says:
  mono subs, audition, profile, D-016's switches) is proposed by an E3 turn, then applied by an
  E3 push: never one gesture, and a push released after turning E3 never applies.
- Acceleration makes master go DOWN faster; it never accelerates master UP (and the Controller
  caps master rises per window: the master-up guard).
"""
from __future__ import annotations

import dataclasses
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Literal, Sequence

from ..state import CHOICES, REBUILD_FIELDS

# ------------------------------------------------------------------------------------------
# Actions (what the controller / dispatcher receives)
# ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Nudge:
    """Move `field` by `detents` steps (the controller owns step sizes and RANGES clamping)."""
    field: str
    detents: int
    fine: bool = False


@dataclass(frozen=True)
class Toggle:
    """Flip a boolean field; on an enum field (`dev_audition`, `profile`) it means 'next choice'."""
    field: str


@dataclass(frozen=True)
class SetChoice:
    """SETUP: set `field` to `value`: what an E3 turn stepped an everyday ON/off to, or what a
    risky row's popup proposed when the E3 push came (D-017; D-013 chose with E2). The
    controller checks it again (profile pinned, rebuild rules) and says so if it refuses."""
    field: str
    value: Any


@dataclass(frozen=True)
class SelectSlot:
    """Move the selected live-EQ slot by `delta` in LIVE_SLOTS order (UI-local, wraps). Kept
    for v0 callers; the knobs use SelectBand."""
    delta: int


@dataclass(frozen=True)
class SelectBand:
    """SHOW, B2 tap: the next band in BAND_CYCLE order (LS 1 2 3 4 HS, wraps). UI-local."""
    delta: int = 1


@dataclass(frozen=True)
class SelectItem:
    """SETUP page: move the selected item (UI-local, clamps). Contract addition."""
    delta: int


@dataclass(frozen=True)
class SelectGroup:
    """SETUP page, E2 (D-017; E3 under D-013): the first item of the next (delta > 0) or previous
    group; back from inside a group first goes to its own first item. UI-local, clamps."""
    delta: int


@dataclass(frozen=True)
class NextPage:
    pass


@dataclass(frozen=True)
class GotoPage:
    """Jump to a named page (B1 tap/hold, SETUP -> HWTEST/PHONE and back)."""
    page: str


MUTE_TOGGLE = "toggle"


@dataclass(frozen=True)
class Mute:
    """on=True/False sets the mute; on='toggle' flips it (only emitted when map_event was not
    told the displayed mute — the dispatcher should always pass it). `seen` (unmute only): the
    mute generation the operator was looking at when the press started (Controller.mute_view);
    the controller refuses the unmute if the mute changed since (a phone muted meanwhile)."""
    on: bool | Literal["toggle"] = MUTE_TOGGLE
    seen: int | None = None


def mute_action(muted: Any = None) -> "Mute":
    """B4 long: the opposite of the mute the screen shows; 'toggle' only if that is unknown.

    Why here and not in the controller: the screen's mute (engine truth, else state) is what
    the operator reacts to; resolving a toggle later against a not-yet-adopted state could
    turn an emergency mute into a no-op.
    """
    if muted is None:
        return Mute(MUTE_TOGGLE)
    try:
        return Mute(not bool(muted))
    except Exception:       # e.g. a numpy array: unknown -> let the controller decide
        return Mute(MUTE_TOGGLE)


@dataclass(frozen=True)
class ResetLive:
    """SETUP > Reset live EQ, confirmed: every live band flat (Controller.reset_live)."""


@dataclass(frozen=True)
class UndoReset:
    """SETUP > Undo reset: put back the live EQ the last reset flattened (Controller)."""


@dataclass(frozen=True)
class SetupAction:
    """SETUP: an action item other than the live-EQ reset/undo ("Laptop access"): the Controller
    runs `Controller.setup_actions[name]`, after the item's confirm if it has one (and the
    confirm is not waived right now: `Focus.no_confirm`)."""
    name: str


@dataclass(frozen=True)
class NotchHelper:
    """SHOW, B3 tap: set a free bell up as a notch (Controller.notch_helper; gain untouched)."""


@dataclass(frozen=True)
class ToggleBand:
    """SHOW, B3 hold (D-014): live band `slot` (a LIVE_SLOTS index: the band selected when the
    hold fired) IN <-> OUT (Controller.toggle_band). OUT keeps the band's settings."""
    slot: int


@dataclass(frozen=True)
class Unlock:
    """ALIGN, B2 hold: E2/E4 (delay, Lo-Cut) editable until `seconds` without input on
    ALIGN. UI-local (Focus.unlock_until)."""
    seconds: float = 30.0          # UNLOCK_S


@dataclass(frozen=True)
class Lock:
    """ALIGN, B2 tap while unlocked: E2/E4 locked again at once. UI-local."""


@dataclass(frozen=True)
class ShowHint:
    text: str
    seconds: float = 2.5          # HINT_S


@dataclass(frozen=True)
class PhoneToggle:
    """PHONE page, E1/E2 push: phone control on/off (web.PhoneServer.toggle). D-007."""


@dataclass(frozen=True)
class PhoneQr:
    """PHONE page, E1 turn: the big QR steps Wi-Fi -> page URL -> first-time setup page."""
    delta: int


Action = (Nudge | Toggle | SetChoice | SelectSlot | SelectBand | SelectItem | SelectGroup
          | NextPage | GotoPage | Mute
          | ResetLive | UndoReset | SetupAction | NotchHelper | ToggleBand | Unlock | Lock
          | ShowHint
          | PhoneToggle | PhoneQr)

# ------------------------------------------------------------------------------------------
# Field names, slots, pages, SETUP items (shared with pages.py)
# ------------------------------------------------------------------------------------------
# Field names are k1000.state field paths ("master_db", "live_peq.2.gain", "out_trim_db.1"), so
# the controller can apply a Nudge with `state.nudge(field, detents, fine)` and a Toggle by
# flipping the bool (or stepping through state.CHOICES for an enum field).
F_MASTER = "master_db"         # Main fader
F_BALANCE = "balance_db"       # macro: Aux1 = +b/2, Aux2 = -b/2
F_SUB_DELAY = "sub_delay_ms"
# E2 on ALIGN (D-011; E3 until D-015): one signed delay, sub - top (> 0 = subs later, < 0 = tops
# later), computed by k1000.state over the two stored delays.
F_ALIGN_DELAY = "align_delay_ms"
F_LOWCUT_FREQ = "lowcut_hz"    # E4 on ALIGN
F_LOWCUT_ON = "lowcut_on"      # SETUP > Lo-Cut (confirmed)
F_SPECTRUM_ON = "spectrum_on"  # SETUP > Spectrum
F_DEHARSH_ON = "deharsh_on"    # SETUP > TOPS > De-harsh (D-016, a rebuild)
F_LOUD_ON = "loud_on"          # SETUP > SUBS > Loudness (D-016, a rebuild)
F_AUX_MODE = "aux_mode"        # SETUP > INPUT > AUX IN 1/2: off | mic | line (D-016, a rebuild)
F_AUX_GAIN = "aux_gain_db"     # SETUP > INPUT > AUX level
F_SLOT = "slot"                # pseudo-field: highlight when the selected band changes

LIVE_SLOTS: tuple[str, ...] = ("live_peq.0", "live_peq.1", "live_peq.2", "live_peq.3",
                               "live_lowshelf", "live_highshelf")
LIVE_SLOT_LABELS: tuple[str, ...] = ("PEQ 1", "PEQ 2", "PEQ 3", "PEQ 4", "LO SHELF", "HI SHELF")
LIVE_SLOT_SHORT: tuple[str, ...] = ("P1", "P2", "P3", "P4", "LS", "HS")
# B2's band order on SHOW, low to high as drawn: LS 1 2 3 4 HS (LIVE_SLOTS indices).
BAND_CYCLE: tuple[int, ...] = (4, 0, 1, 2, 3, 5)
BELL_SLOTS: tuple[int, ...] = (0, 1, 2, 3)      # the notch helper's candidates (never a shelf)

PAGES: tuple[str, ...] = ("SHOW", "ALIGN", "SETUP")   # the main pages; HWTEST/PHONE via SETUP
ALL_PAGES: tuple[str, ...] = PAGES + ("HWTEST", "PHONE")   # PHONE via SETUP > Phone (D-007)
HOME_PAGE = "SHOW"
# v0 page names, still accepted where a page is named (console `page MAIN`, old callers).
PAGE_ALIASES: dict[str, str] = {"MAIN": "ALIGN", "LIVE": "SHOW"}
AUTO_HOME_PAGES: tuple[str, ...] = ("ALIGN", "SETUP")
# B1 (D-013): a tap flips SHOW <-> ALIGN and steps back from SETUP (to SHOW) and PHONE (to
# SETUP, where it was opened); a hold opens SETUP from SHOW/ALIGN and goes home from the others.
B1_TAP: dict[str, str] = {"SHOW": "ALIGN", "ALIGN": "SHOW", "SETUP": "SHOW", "PHONE": "SETUP"}
B1_HOLD: dict[str, str] = {"SHOW": "SETUP", "ALIGN": "SETUP"}
HOME_AFTER_S = 60.0           # ALIGN/SETUP -> SHOW after this long without a knob/button event
UNLOCK_S = 30.0               # ALIGN E2/E4 unlock, renewed by every input on ALIGN


def page_name(page: Any) -> Any:
    """A page name with the v0 aliases resolved (MAIN -> ALIGN, LIVE -> SHOW)."""
    return PAGE_ALIASES.get(page, page) if isinstance(page, str) else page


def live_field(slot: int, param: str) -> str:
    """'live_peq.1.freq' etc. — slot index wraps into LIVE_SLOTS."""
    return f"{LIVE_SLOTS[slot % len(LIVE_SLOTS)]}.{param}"


def next_band(slot: int, delta: int = 1) -> int:
    """The slot `delta` steps after `slot` in BAND_CYCLE order (wraps)."""
    try:
        i = BAND_CYCLE.index(slot % len(LIVE_SLOTS))
    except (TypeError, ValueError):
        i = 0
    return BAND_CYCLE[(i + delta) % len(BAND_CYCLE)]


@dataclass(frozen=True)
class SetupItem:
    """One SETUP row (knobs D-017). kind: num (an E3 turn moves it), bool / choice (an E3 turn
    steps it, applied at once; on a `risky` row the turn only proposes, in a popup, and an E3
    push applies), page (an E3 push opens it), action (an E3 push runs it: `field` names the
    action). `confirm` (actions): the E3 push must come twice within CONFIRM_WINDOW_S. `group`:
    its SETUP_GROUPS name, filled in by SETUP_ITEMS."""
    label: str
    field: str
    kind: Literal["num", "bool", "choice", "page", "action"]
    confirm: bool = False
    group: str = ""

    @property
    def needs_confirm(self) -> bool:
        return self.confirm and self.kind == "action"

    @property
    def picks(self) -> bool:
        """An ON/off or a choice (an E3 turn steps it; D-013 chose these with E2)."""
        return self.kind in ("bool", "choice")

    @property
    def risky(self) -> bool:
        """D-017: an E3 turn only proposes (the popup) and an E3 push applies. The rows named in
        SETUP_RISKY, and every pipeline rebuild whatever that list says: a rebuild is never one
        gesture."""
        return self.picks and (self.field in SETUP_RISKY or self.rebuild)

    @property
    def rebuild(self) -> bool:
        """Setting it rebuilds the pipeline (a possible click): said in the popup."""
        return self.field in REBUILD_FIELDS

    @property
    def choices(self) -> tuple[Any, ...]:
        if self.kind == "bool":
            return (False, True)
        return tuple(CHOICES.get(self.field, ()))


A_RESET_LIVE = "reset_live"     # SETUP action items (`SetupItem.field`)
A_UNDO_RESET = "undo_reset"
A_LAPTOP = "laptop_access"      # the Friday tune's laptop over the K1000 Wi-Fi (docs/06 §11)

# SETUP (D-013): grouped by speaker floor, then the rest by what it serves. The ALIGN page keeps
# what is set per venue (balance, the signed delay, the Lo-Cut frequency); SETUP holds what is set
# once per rig. D-017: E3 is the value knob; a risky row (SETUP_RISKY below) is proposed by an E3
# turn and applied by an E3 push (the popup shows both values), so a pipeline rebuild (Mono,
# Profile, Audition, and D-016's De-harsh, Loudness and AUX IN: a possible click) is never one
# gesture. Laptop access opens with a double E3 push and closes with one (the Controller waives
# its confirm while it is open).
# D-016 rows sit in the group of what they act on, each switch right before its numbers (the phone
# pairs a switch with the next number whose label starts with the switch's). Labels fit the 480 px
# row beside their longest value at 20 px ("De-harsh thresh  -48.0 dB"; tests/test_ui.py checks it).
SETUP_GROUPS: tuple[tuple[str, tuple[SetupItem, ...]], ...] = (
    ("TOPS", (
        SetupItem("Trim L", "out_trim_db.2", "num"),
        SetupItem("Trim R", "out_trim_db.3", "num"),
        SetupItem("Polarity", "top_inverted", "bool"),
        SetupItem("Lo-Cut", F_LOWCUT_ON, "bool"),          # its frequency: ALIGN E4
        # D-016: a dynamic cut around the harsh band (2.7 kHz) that acts only on loud attacks
        SetupItem("De-harsh", F_DEHARSH_ON, "bool"),
        SetupItem("De-harsh freq", "deharsh_hz", "num"),
        SetupItem("De-harsh thresh", "deharsh_thresh_db", "num"),
    )),
    ("SUBS", (
        SetupItem("Trim L", "out_trim_db.0", "num"),
        SetupItem("Trim R", "out_trim_db.1", "num"),
        SetupItem("Polarity", "sub_inverted", "bool"),
        SetupItem("Mono L+R", "mono_subs", "bool"),
        SetupItem("Low-pass", "sub_lowpass_on", "bool"),
        SetupItem("Low-pass freq", "sub_lowpass_hz", "num"),
        # D-016: the subs' low shelf grows as the master goes under the reference level
        SetupItem("Loudness", F_LOUD_ON, "bool"),
        SetupItem("Loudness boost", "loud_boost_db", "num"),
        SetupItem("Loudness ref", "loud_ref_db", "num"),
    )),
    ("INPUT", (
        SetupItem("Trim", "input_trim_db", "num"),
        # D-016: the M4's IN1 (a mic) or IN1 + IN2 (a line pair) into the program, after the trim
        SetupItem("AUX IN 1/2", F_AUX_MODE, "choice"),
        SetupItem("AUX level", "aux_gain_db", "num"),
    )),
    ("LIVE EQ", (
        SetupItem("Reset live EQ", A_RESET_LIVE, "action", confirm=True),
        SetupItem("Undo reset", A_UNDO_RESET, "action"),
        SetupItem("Spectrum", F_SPECTRUM_ON, "bool"),
    )),
    ("PHONE", (
        SetupItem("Phone", "PHONE", "page"),          # QR codes + phone on/off (D-007)
        SetupItem("Laptop access", A_LAPTOP, "action", confirm=True),
    )),
    ("BOX", (
        SetupItem("Profile", "profile", "choice"),
        SetupItem("Audition (dev)", "dev_audition", "choice"),
        SetupItem("Knob test", "HWTEST", "page"),
    )),
)
SETUP_ITEMS: tuple[SetupItem, ...] = tuple(
    dataclasses.replace(it, group=name) for name, items in SETUP_GROUPS for it in items)
# D-017, the risky rows: an E3 turn opens a popup (`now -> proposed`) and only an E3 push applies
# it. Every pipeline rebuild (Mono L+R, Profile, Audition, De-harsh, Loudness, AUX IN 1/2: a
# possible click; SetupItem.risky keeps them risky even if this line forgets one), both
# polarities (the floor dips, the sub/top sum changes) and Lo-Cut (off sends full range to the
# tops). Every other ON/off (Low-pass, Spectrum) is applied as E3 turns. Claude's proposal
# (2026-10-17): Thomas changes the list here, one line.
SETUP_RISKY: frozenset[str] = REBUILD_FIELDS | {"top_inverted", "sub_inverted", F_LOWCUT_ON}
SETUP_GROUP_NAMES: tuple[str, ...] = tuple(name for name, _items in SETUP_GROUPS)


def setup_group_start(i: int) -> int:
    """Index (into SETUP_ITEMS) of the first item of item `i`'s group."""
    i = max(0, min(len(SETUP_ITEMS) - 1, i))
    g = SETUP_ITEMS[i].group
    while i > 0 and SETUP_ITEMS[i - 1].group == g:
        i -= 1
    return i


def setup_group_pos(i: int) -> tuple[str, int, int]:
    """(group name, 1-based position in it, its size) of item `i`."""
    i = max(0, min(len(SETUP_ITEMS) - 1, i))
    first = setup_group_start(i)
    g = SETUP_ITEMS[i].group
    size = sum(1 for it in SETUP_ITEMS if it.group == g)
    return g, i - first + 1, size


def setup_group_step(i: int, delta: int) -> int:
    """E2 on SETUP (D-017; E3 under D-013): the first item `delta` groups away (clamped).
    Backwards from inside a group, the first step lands on that group's own first item."""
    i = max(0, min(len(SETUP_ITEMS) - 1, i))
    first = setup_group_start(i)
    if delta < 0 and i != first:
        i, delta = first, delta + 1
    starts = [k for k in range(len(SETUP_ITEMS)) if k == 0 or SETUP_ITEMS[k - 1].group != SETUP_ITEMS[k].group]
    g = starts.index(setup_group_start(i))
    return starts[max(0, min(len(starts) - 1, g + delta))]


def choice_name(item: SetupItem, value: Any) -> str:
    """How the box screen names one value of an ON/off or choice row (pages._fmt_setup's words):
    polarity normal / INVERTED, other ON/off rows off / ON, choices upper case, except a choice's
    "off" (AUX IN's), which reads like every other off: lower case, the quiet state."""
    if item.field.endswith("_inverted"):
        return "INVERTED" if value else "normal"
    if item.kind == "bool" or isinstance(value, bool):
        return "ON" if value else "off"
    if value == "off":
        return "off"
    return str(value).upper()


def setup_schema() -> list[dict[str, Any]]:
    """SETUP_GROUPS as plain data, for the phone (protocol 2, docs/14 §3.1.3): one entry per
    group, `{group, items: [{label, f, kind, ...}]}` in the box's order. ON/off and choice rows
    carry their `choices`, the box's `names` for them and `rebuild` (a pipeline rebuild: may
    click); action rows their `confirm`. Data only: nothing here changes what the knobs do."""
    out: list[dict[str, Any]] = []
    for name, items in SETUP_GROUPS:
        rows: list[dict[str, Any]] = []
        for it in items:
            row: dict[str, Any] = {"label": it.label, "f": it.field, "kind": it.kind}
            if it.picks:
                row["choices"] = list(it.choices)
                row["names"] = [choice_name(it, c) for c in it.choices]
                row["rebuild"] = it.rebuild
            elif it.kind == "action":
                row["confirm"] = it.needs_confirm
            rows.append(row)
        out.append({"group": name, "items": rows})
    return out


@dataclass(frozen=True)
class Knob:
    """One encoder's meaning on one page. `field` may contain '{slot}' (the selected band)."""
    label: str
    field: str | None                 # None = unused on this page
    accel_up: int = 1                 # max detent multiplier on fast turns, per direction
    accel_down: int = 1
    kind: Literal["nudge", "slot", "item", "group", "setup_value", "phone_qr"] = "nudge"
    # The first detent after ARM_WINDOW_S of rest only wakes the knob (highlight + hint); the
    # next ones edit. For a value whose every change is audible as a dip (the delay).
    arm: bool = False
    # ALIGN alignment knobs: a turn does nothing (HINT_LOCKED) until a B2 hold unlocks them.
    locked: bool = False


# E1..E4 per page — the contract's knob table, as data. D-015 (Thomas on the box, 2026-10-14):
# SHOW's Q on E3 and GAIN on E4 (GAIN was E3); ALIGN's DELAY on E2 and BALANCE on E3 (the other
# way round before), so the two LOCKED knobs, DELAY and LO-CUT, are the two bottom ones.
# D-017 (Thomas at the rig, 2026-10-17): SETUP's VALUE on E3 (turn = change, push = set / open /
# run) and GROUP on E2 (E2 VALUE and E3 GROUP under D-013).
KNOBS: dict[str, tuple[Knob, Knob, Knob, Knob]] = {
    "SHOW": (
        Knob("MASTER", F_MASTER, accel_up=1, accel_down=4),
        Knob("FREQ", "{slot}.freq", accel_up=4, accel_down=4),
        Knob("Q", "{slot}.q"),
        Knob("GAIN", "{slot}.gain"),
    ),
    "ALIGN": (
        Knob("MASTER", F_MASTER, accel_up=1, accel_down=4),
        Knob("DELAY", F_ALIGN_DELAY, arm=True, locked=True),
        Knob("BALANCE", F_BALANCE),
        Knob("LO-CUT", F_LOWCUT_FREQ, accel_up=4, accel_down=4, locked=True),
    ),
    "SETUP": (
        Knob("ITEM", None, kind="item"),
        Knob("GROUP", None, kind="group"),
        Knob("VALUE", None, kind="setup_value"),
        Knob("", None),
    ),
    "HWTEST": (Knob("", None), Knob("", None), Knob("", None), Knob("", None)),
    "PHONE": (Knob("QR", None, kind="phone_qr"), Knob("", None), Knob("", None), Knob("", None)),
}


def knob_accel(field: str) -> tuple[int, int] | None:
    """(accel_up, accel_down) of the SHOW/ALIGN knob that edits `field` (a band field matches
    its '{slot}' pattern by parameter: 'live_peq.2.freq' -> FREQ), None when no knob there edits
    it. Data for the phone's slide (docs/14 §3.1.1); the knobs read KNOBS themselves."""
    param = field.rsplit(".", 1)[-1] if field.startswith(("live_peq", "live_lowshelf",
                                                          "live_highshelf")) else None
    for page in ("SHOW", "ALIGN"):
        for k in KNOBS[page]:
            if k.field is None or k.kind != "nudge":
                continue
            if k.field == field or (param is not None and k.field == "{slot}." + param):
                return k.accel_up, k.accel_down
    return None

# Screen corner of each logical encoder: E1 top-left, E2 bottom-left, E3 top-right, E4
# bottom-right. Confirmed by Thomas on the box on 2026-10-12 (each knob sits at its label's
# corner; 10 clicks = +10). Another shell with the knobs elsewhere: correct this table only.
KNOB_CORNERS: tuple[str, str, str, str] = ("tl", "bl", "tr", "br")

# The "E1".."E4" tag drawn next to each knob's label (k1000.yaml `ui.knob_tags`, D-011):
# dim = small and dark grey, secondary to the label (default); off = no tag, the label takes
# the corner (a sticker on the shell names the knobs); bright = the v1 cyan tag.
KNOB_TAG_STYLES: tuple[str, ...] = ("dim", "off", "bright")
KNOB_TAGS_DEFAULT = "dim"


def parse_knob_tags(value: Any) -> str | None:
    """One `knob_tags` value -> 'dim' | 'off' | 'bright', or None when it is not one of them.
    YAML reads a bare `off` as False (and `on` as True): False = off, True = the default."""
    if value is False:
        return "off"
    if value is True:
        return KNOB_TAGS_DEFAULT
    if isinstance(value, str) and value.strip().lower() in KNOB_TAG_STYLES:
        return value.strip().lower()
    return None


def knob_tag_style(cfg: Any) -> str:
    """k1000.yaml `ui.knob_tags`; missing or invalid -> KNOB_TAGS_DEFAULT (never an error: a
    typo here must not cost the screen)."""
    sec = cfg.get("ui") if isinstance(cfg, dict) else None
    v = sec.get("knob_tags") if isinstance(sec, dict) else None
    return parse_knob_tags(v) or KNOB_TAGS_DEFAULT

# Acceleration: turn rate (detents/s) -> multiplier, the knob driver's curve (accel_multiplier)
# with its onset raised here: x1 up to 20 det/s, the knob's cap from 40 det/s. The rate is the
# slower of the last TWO detent intervals, so a single quick interval (a flick, sampling
# jitter) never jumps, and the first two detents of any turn are x1. A direction change or a
# pause longer than the window resets to x1, so correcting an overshoot is always
# fine-grained. Applied HERE only (per-field caps); the controller must not accelerate again.
# docs/08-knob-precision.md: 12 det/s (v0) is a normal brisk turn of a 24-click knob.
ACCEL_WINDOW_S = 0.25
ACCEL_LOW_DPS = 20.0
ACCEL_HIGH_DPS = 40.0

try:
    from ..hw.mcp23017 import accel_multiplier
except Exception:  # pragma: no cover - keeps the UI usable if the driver module breaks
    def accel_multiplier(rate_dps: float, low: float = 12.0, high: float = 40.0,
                         max_mult: int = 4) -> int:
        max_mult = max(1, int(max_mult))
        if rate_dps <= low:
            return 1
        if rate_dps >= high or high <= low:
            return max_mult
        return 1 + int((max_mult - 1) * (rate_dps - low) / (high - low))

HINT_MUTE = "HOLD B4 TO MUTE / UNMUTE"
HINT_LOCKED = "LOCKED · HOLD B2 TO EDIT"
HINT_UNLOCKED = "DELAY + LO-CUT UNLOCKED"
HINT_RELOCKED = "DELAY + LO-CUT LOCKED"
HINT_B2_OFF_SHOW = "B2: NEXT BAND ON SHOW"
HINT_B3_OFF_SHOW = "B3: NOTCH HELPER ON SHOW"
HINT_RESET_IN_SETUP = "RESET LIVE EQ: IN SETUP"     # B3 hold on SHOW until D-014 (now: band IN/OUT)
# D-014's gate: a B3 hold on SHOW while the button map is unconfirmed (it names the button, so an
# operator who meant B4 MUTE sees which one he held).
HINT_B3_HOLD_UNCONFIRMED = "B3 HOLD: IN/OUT AFTER KNOB TEST"
# SETUP (D-017): every hint names E3, the one knob that changes, opens or runs anything there;
# an E1 or E2 push says which E3 gesture the row takes.
HINT_TURN_VALUE = "TURN E3 TO CHANGE"
HINT_PUSH_OPEN = "PUSH E3 TO OPEN"
HINT_PUSH_RUN = "PUSH E3 TO RUN"
HINT_PUSH_TWICE = "PUSH E3 TWICE TO RUN"
HINT_PUSH_APPLY = "PUSH E3 TO APPLY"
HINT_CANCELLED = "CANCELLED"
HINT_ALREADY = "ALREADY {}"     # an E3 turn towards the value the row already holds
HINT_ARM = "{} · TURN AGAIN TO CHANGE"
CONFIRM_WINDOW_S = 5.0
PICK_WINDOW_S = 8.0           # SETUP: a proposal waits this long for its E3 push (each turn renews)
# SETUP: an E3 push applies a proposal only if it was PRESSED at least this long after the
# proposal last moved. A detent clicked by the finger pressing the knob (or a turn and a push
# made as one gesture) would otherwise propose and apply in one go, the popup never seen
# (D-017 review). Refused, the popup stays and says PUSH E3 TO APPLY.
PICK_SETTLE_S = 0.25
SETUP_VALUE_KNOB = 2          # E3 (D-017): SETUP's value knob, its push the only one that acts
# D-017: what the popup says on an action's first E3 push (question, consequence, what the second
# push does); another confirmed action asks "<label>?".
SETUP_ASK: dict[str, tuple[str, str, str]] = {
    A_RESET_LIVE: ("Reset live EQ?", "ALL 6 LIVE BANDS FLAT · UNDO 5 MIN", "reset"),
    A_LAPTOP: ("Open laptop access?", "GUI + SSH OVER THE K1000 WI-FI · 2 H", "open"),
}

# ------------------------------------------------------------------------------------------
# Physical -> logical mapping (from /etc/k1000/k1000.yaml, section `controls:`)
# ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ControlMap:
    """encoders[i] = physical encoder index of E(i+1); buttons[i] = switch index of B(i+1);
    pushes[i] = switch index of E(i+1)'s push; reverse[i] flips E(i+1)'s direction.

    `buttons_confirmed` (D-014's gate): someone proved which physical button is B1..B4 (SETUP >
    Knob test) and wrote it down. Read as `k1000-m4-bringup.sh` reads it: a `buttons` map other
    than the default is a hand-written one; the default map counts only with an explicit
    `buttons_confirmed: true` (a real YAML bool) next to it, and never when the `buttons` the
    file gives was invalid (the map in use is then not the one that was confirmed)."""
    encoders: tuple[int, int, int, int] = (0, 1, 2, 3)
    buttons: tuple[int, int, int, int] = (4, 5, 6, 7)
    pushes: tuple[int, int, int, int] = (0, 1, 2, 3)
    reverse: tuple[bool, bool, bool, bool] = (False, False, False, False)
    buttons_confirmed: bool = False

    @classmethod
    def from_cfg(cls, cfg: Any) -> "ControlMap":
        """Read `controls:` (or `hw:`) from the k1000.yaml dict; anything invalid -> default.

        Why lenient: a typo in the YAML must not leave the box without knobs on a gig; it logs
        nothing here (pure) — the caller can compare against `ControlMap()` and warn.
        """
        if isinstance(cfg, ControlMap):
            return cfg
        sec = {}
        if isinstance(cfg, dict):
            sec = cfg.get("controls") or cfg.get("hw") or {}
        if not isinstance(sec, dict):
            sec = {}
        d = cls()

        def strict_int(x: Any) -> int:
            # YAML ints only: True, 1.7 or "1" are typos, not indices
            if isinstance(x, bool) or not isinstance(x, int):
                raise ValueError(x)
            return x

        def strict_bool(x: Any) -> bool:
            # real bools or 0/1 only: bool("false") would be True and reverse a knob
            if isinstance(x, bool):
                return x
            if isinstance(x, int) and x in (0, 1):
                return bool(x)
            raise ValueError(x)

        def four(key: str, default: tuple, conv, valid) -> tuple:
            v = sec.get(key, default)
            try:
                t = tuple(conv(x) for x in v)
            except (TypeError, ValueError):
                return default
            return t if len(t) == 4 and all(valid(x) for x in t) else default

        enc = four("encoders", d.encoders, strict_int, lambda x: 0 <= x <= 3)
        btn = four("buttons", d.buttons, strict_int, lambda x: 0 <= x <= 7)
        psh = four("pushes", d.pushes, strict_int, lambda x: 0 <= x <= 7)
        rev = four("reverse", d.reverse, strict_bool, lambda x: True)
        if len(set(enc)) != 4:
            enc = d.encoders
        if len(set(btn + psh)) != 8:
            btn, psh = d.buttons, d.pushes
        # D-014's gate. The file's own `buttons`, as written (None = not given): a confirmation
        # covers the map in use only if that is what the file says (or it says nothing).
        said = sec.get("buttons")
        took = said is None or (isinstance(said, (list, tuple)) and len(said) == 4
                                and all(type(x) is int for x in said) and tuple(said) == btn)
        flag = sec.get("buttons_confirmed", False) is True       # a real bool: "yes" is a typo
        confirmed = took and (btn != d.buttons or flag)
        return cls(enc, btn, psh, rev, confirmed)

    def encoder_role(self, phys: int) -> int | None:
        """Physical encoder index -> logical 0..3 (E1..E4), or None."""
        return self.encoders.index(phys) if phys in self.encoders else None

    def switch_role(self, phys: int) -> tuple[str, int] | None:
        """Physical switch index -> ('B', 0..3) or ('P', 0..3) (encoder push), or None."""
        if phys in self.buttons:
            return ("B", self.buttons.index(phys))
        if phys in self.pushes:
            return ("P", self.pushes.index(phys))
        return None


# ------------------------------------------------------------------------------------------
# Focus: UI-local state shared by input_map (writes) and pages (reads)
# ------------------------------------------------------------------------------------------
EDIT_HIGHLIGHT_S = 2.5
HINT_S = 2.5
ARM_WINDOW_S = 2.5        # an `arm` knob at rest this long needs a wake-up detent again


@dataclass
class HwTestLog:
    """What the HWTEST page shows: raw physical activity, to identify knob positions."""
    enc_count: list[int] = field(default_factory=lambda: [0, 0, 0, 0])
    sw_down: list[bool] = field(default_factory=lambda: [False] * 8)
    sw_last: list[str] = field(default_factory=lambda: [""] * 8)
    lines: deque = field(default_factory=lambda: deque(maxlen=4))
    last: str = ""
    # current logical role of each physical control (refreshed from the ControlMap)
    enc_role: list[str] = field(default_factory=lambda: ["E1", "E2", "E3", "E4"])
    sw_role: list[str] = field(default_factory=lambda: ["E1 push", "E2 push", "E3 push",
                                                        "E4 push", "B1", "B2", "B3", "B4"])

    def refresh_roles(self, cmap: "ControlMap") -> None:
        self.enc_role = [f"E{r + 1}" if (r := cmap.encoder_role(i)) is not None else "-"
                         for i in range(4)]
        roles = []
        for i in range(8):
            role = cmap.switch_role(i)
            roles.append("-" if role is None else
                         (f"B{role[1] + 1}" if role[0] == "B" else f"E{role[1] + 1} push"))
        self.sw_role = roles


@dataclass
class Focus:
    slot: int = 0                         # SHOW: the selected band (index into LIVE_SLOTS)
    item: int = 0                         # SETUP: index into SETUP_ITEMS
    editing: str | None = None            # field to highlight (last nudged/toggled)
    edit_until: float = 0.0               # time.monotonic() deadline of the highlight
    hint: str | None = None
    hint_until: float = 0.0
    held: set[int] = field(default_factory=set)          # logical encoders whose push is down
    turned: set[int] = field(default_factory=set)        # held pushes whose knob was turned
    last_turn: dict[int, tuple[float, int]] = field(default_factory=dict)  # enc -> (ts, sign)
    turn_dt: dict[int, float | None] = field(default_factory=dict)  # enc -> previous interval
    confirm: str | None = None            # SETUP action waiting for its second E3 push (the popup)
    confirm_until: float = 0.0
    # SETUP (D-017): the value an E3 turn proposed for `pick` (a risky row's field), applied by
    # the next E3 push before pick_until; None = nothing proposed. The popup shows
    # `now -> proposed`.
    pick: str | None = None
    pick_value: Any = None
    pick_until: float = 0.0
    pick_at: float = 0.0                  # when the proposal was opened or last moved
    pressed_at: dict[int, float] = field(default_factory=dict)   # enc -> its push's last press
    hw: HwTestLog = field(default_factory=HwTestLog)
    # B4 press (mute, D-013): (the mute B4 acts against, its generation) as the operator saw
    # them when the press STARTED. B4 long fires 0.8 s later, while still held: it acts on that.
    b2_seen: tuple[Any, Any] | None = None
    # ALIGN: E2/E4 (delay, Lo-Cut) editable until this deadline (time.monotonic()); 0 = locked.
    unlock_until: float = 0.0
    # SETUP items whose confirm is waived right now (set by the Controller): Laptop access
    # while it is open, so that closing it, the safe direction, is a single push.
    no_confirm: set[str] = field(default_factory=set)
    # SETUP fields that cannot change right now -> the hint a turn or push shows instead (set by
    # the Controller: Profile pinned in k1000.yaml, Audition off the dev profile, Spectrum
    # disabled in k1000.yaml, AUX IN and its level on a profile with no spare input). The row
    # draws its value dimmed.
    readonly: dict[str, str] = field(default_factory=dict)

    def editing_now(self, now: float | None = None) -> str | None:
        now = time.monotonic() if now is None else now
        return self.editing if self.editing and now < self.edit_until else None

    def hint_now(self, now: float | None = None) -> str | None:
        now = time.monotonic() if now is None else now
        return self.hint if self.hint and now < self.hint_until else None

    def confirm_now(self, now: float | None = None) -> str | None:
        now = time.monotonic() if now is None else now
        return self.confirm if self.confirm and now < self.confirm_until else None

    def pick_now(self, now: float | None = None) -> tuple[str, Any] | None:
        """(field, proposed value) while a SETUP proposal waits for its E3 push, else None."""
        now = time.monotonic() if now is None else now
        return (self.pick, self.pick_value) if self.pick and now < self.pick_until else None

    def clear_pick(self) -> None:
        self.pick, self.pick_value, self.pick_until, self.pick_at = None, None, 0.0, 0.0

    def unlocked(self, now: float | None = None) -> bool:
        """ALIGN's E2/E4 (delay, Lo-Cut) are editable now."""
        now = time.monotonic() if now is None else now
        return now < self.unlock_until

    def unlock_left(self, now: float | None = None) -> float:
        """Seconds left of the ALIGN unlock (0.0 = locked)."""
        now = time.monotonic() if now is None else now
        return max(0.0, self.unlock_until - now)

    @property
    def band(self) -> int:
        """The selected band as a LIVE_SLOTS index (always valid)."""
        try:
            return int(self.slot) % len(LIVE_SLOTS)
        except (TypeError, ValueError):
            return 0


def apply_focus(focus: Focus, action: Action, now: float | None = None) -> bool:
    """Apply the UI-local part of `action` to `focus`. True = fully handled (do not forward).

    Nudge/Toggle also set the edit highlight but return False: the controller must act on them.
    """
    now = time.monotonic() if now is None else now
    if isinstance(action, SelectSlot):
        focus.slot = (focus.band + action.delta) % len(LIVE_SLOTS)
        focus.editing, focus.edit_until = F_SLOT, now + EDIT_HIGHLIGHT_S
        return True
    if isinstance(action, SelectBand):
        focus.slot = next_band(focus.band, action.delta)
        focus.editing, focus.edit_until = F_SLOT, now + EDIT_HIGHLIGHT_S
        return True
    if isinstance(action, SelectItem):
        focus.item = max(0, min(len(SETUP_ITEMS) - 1, focus.item + action.delta))
        focus.confirm = None
        focus.clear_pick()
        return True
    if isinstance(action, SelectGroup):
        focus.item = setup_group_step(focus.item, action.delta)
        focus.confirm = None
        focus.clear_pick()
        return True
    if isinstance(action, Unlock):
        focus.unlock_until = now + max(0.0, float(action.seconds))
        focus.hint, focus.hint_until = HINT_UNLOCKED, now + HINT_S
        return True
    if isinstance(action, Lock):
        focus.unlock_until = 0.0
        focus.hint, focus.hint_until = HINT_RELOCKED, now + HINT_S
        return True
    if isinstance(action, ShowHint):
        focus.hint, focus.hint_until = action.text, now + action.seconds
        return True
    if isinstance(action, (Nudge, Toggle, SetChoice)):
        focus.editing, focus.edit_until = action.field, now + EDIT_HIGHLIGHT_S
        return False
    return False


# ------------------------------------------------------------------------------------------
# The mapping
# ------------------------------------------------------------------------------------------


def _is_encoder(event: Any) -> bool:
    return hasattr(event, "delta")


def _accel(knob: Knob, delta: int, last: tuple[float, int] | None, now: float,
           fine: bool, prev_dt: float | None = None) -> int:
    """Scale `delta` by turn rate; x1 when fine, reversing, after a pause, for flat knobs, and
    until two intervals in a row (`prev_dt`, then this one) say the turn is fast.

    MASTER up is never accelerated, on any page, whatever its Knob says (D-009 master-up guard:
    the level may only rise one detent per click; going down stays quick)."""
    cap = knob.accel_up if delta > 0 else knob.accel_down
    if knob.field == F_MASTER and delta > 0:
        cap = 1
    sign = 1 if delta > 0 else -1
    if fine or cap <= 1 or last is None or last[1] != sign or prev_dt is None:
        return delta
    dt = now - last[0]
    if dt > ACCEL_WINDOW_S or prev_dt > ACCEL_WINDOW_S:
        return delta
    rate = abs(delta) / max(dt, prev_dt, 0.002)
    return delta * accel_multiplier(rate, ACCEL_LOW_DPS, ACCEL_HIGH_DPS, cap)


def map_event(page: str, focus: Focus, event: Any, cfg: Any = None, *,
              muted: bool | None = None, mute_seq: int | None = None,
              state: Any = None) -> list[Action]:
    """Map one hardware event to actions for `page` (v0 names MAIN/LIVE are aliases).

    `event` is duck-typed: an encoder event has `.index`, `.delta` (signed detents) and
    optionally `.ts` (monotonic seconds); a switch event has `.index` and `.kind` in
    {'press', 'release', 'short', 'long'}. `cfg` is the k1000.yaml dict (or a ControlMap).
    `muted` is the mute the screen currently shows (`pages.displayed_muted(state, status)`):
    B4 long then emits `Mute(not muted)`; with None it falls back to `Mute('toggle')`.
    `mute_seq` is its generation (Controller.mute_view). Both are recorded at the B4 PRESS: the
    long press acts on what the operator meant when the finger went down (a mute stays a mute
    whatever a phone did meanwhile; an unmute carries `seen` and is refused if the mute changed).
    `state` (k1000.state.State or anything with `.get(field)`): the values SETUP chooses from.
    """
    page = page_name(page)
    cmap = ControlMap.from_cfg(cfg)
    now = getattr(event, "ts", None)
    now = time.monotonic() if now is None else float(now)

    if page == "HWTEST":
        return _hwtest(focus, event, cmap, muted, mute_seq)

    # ALIGN's unlock lasts UNLOCK_S of idle: every input on ALIGN renews it.
    if page == "ALIGN" and focus.unlocked(now):
        focus.unlock_until = now + UNLOCK_S

    if _is_encoder(event):
        e = cmap.encoder_role(int(event.index))
        if e is None or not event.delta:
            return []
        delta = int(event.delta) * (-1 if cmap.reverse[e] else 1)
        knobs = KNOBS.get(page)
        if knobs is None:
            return []
        knob = knobs[e]
        if knob.locked and not focus.unlocked(now):
            # Nothing recorded: a locked turn must not count as the arm knob's wake-up.
            return [ShowHint(HINT_LOCKED)]
        last = focus.last_turn.get(e)
        sign = 1 if delta > 0 else -1
        prev_dt = focus.turn_dt.get(e)
        focus.turn_dt[e] = now - last[0] if last is not None and last[1] == sign else None
        focus.last_turn[e] = (now, sign)
        fine = e in focus.held
        if fine:
            focus.turned.add(e)       # its release is the end of a fine turn, not a 'select'
        if knob.kind in ("item", "group") and setup_popup(focus, now) is not None:
            # D-017: the popup says "turn E1 = cancel". An E1 (or E2) turn closes it and moves
            # nothing: the row it named stays selected, nothing applied.
            focus.clear_pick()
            focus.confirm = None
            return [ShowHint(HINT_CANCELLED)]
        # Slot/item: every detent counts (the driver merges a fast turn into one event).
        if knob.kind == "slot":
            return [SelectSlot(delta)]
        if knob.kind == "item":
            return [SelectItem(delta)]
        if knob.kind == "group":
            return [SelectGroup(1 if delta > 0 else -1)]
        if knob.kind == "setup_value":
            return _setup_turn(focus, delta, fine, now, state)
        if knob.kind == "phone_qr":
            return [PhoneQr(1 if delta > 0 else -1)]
        if knob.field is None:
            return []
        fld = knob.field.replace("{slot}", LIVE_SLOTS[focus.band])
        if knob.arm and not fine and (last is None or now - last[0] > ARM_WINDOW_S):
            focus.editing, focus.edit_until = fld, now + EDIT_HIGHLIGHT_S
            return [ShowHint(HINT_ARM.format(knob.label))]
        return [Nudge(fld, _accel(knob, delta, last, now, fine, prev_dt), fine)]

    # switches
    role = cmap.switch_role(int(event.index))
    if role is None:
        return []
    kind = getattr(event, "kind", "")
    group, n = role
    if group == "P":
        turned = n in focus.turned
        if kind == "press":
            focus.held.add(n)
            focus.turned.discard(n)
            focus.pressed_at[n] = now
        elif kind == "release":
            # The driver sends `release` THEN `short` (hw/mcp23017.py): `turned` must survive
            # the release so the short that follows still knows the knob was turned.
            focus.held.discard(n)
        elif kind == "short":
            focus.held.discard(n)
            focus.turned.discard(n)
        # A push released after turning its knob (the fine-mode habit from SHOW) is not a
        # 'select': on SETUP it would apply the value the E3 turn proposed, or open a page.
        if page == "PHONE" and kind == "short" and n in (0, 1) and not turned:
            return [PhoneToggle()]
        if page == "SETUP" and kind == "short" and not turned:
            if n == SETUP_VALUE_KNOB:           # D-017: E3's push sets, opens, runs
                return _setup_push(focus, now, state)
            if n in (0, 1):                     # E1/E2 pushes only say what E3 does here
                return [ShowHint(_setup_e3_hint(focus, now))]
        return []

    if n == 0:   # B1 (D-013): tap (on release) = SHOW <-> ALIGN / back; hold = SETUP / home
        if kind == "short":
            return [GotoPage(B1_TAP.get(page, HOME_PAGE))]
        return [GotoPage(B1_HOLD.get(page, HOME_PAGE))] if kind == "long" else []
    if n == 1:   # B2: next band (SHOW), unlock/lock the alignment knobs (ALIGN)
        return _band_button(page, focus, kind, now)
    if n == 2:   # B3: the notch helper (SHOW tap), the selected band IN/OUT (SHOW hold, D-014)
        if page == "SHOW":
            if kind == "short":
                return [NotchHelper()]
            if kind != "long":
                return []
            # Only on a confirmed map: on a swapped one this "B3" may be the B4 the operator
            # holds to MUTE, and a band (the notch fighting the ring) going OUT is the opposite.
            return ([ToggleBand(focus.band)] if cmap.buttons_confirmed
                    else [ShowHint(HINT_B3_HOLD_UNCONFIRMED)])
        return [ShowHint(HINT_B3_OFF_SHOW)] if kind == "short" else []
    if n == 3:   # B4 mute — long press only, on the intent of its press
        return _b2(focus, kind, muted, mute_seq)
    return []


def _band_button(page: str, focus: Focus, kind: str, now: float) -> list[Action]:
    """B2 (D-013, was B3): next band on SHOW; on ALIGN a hold unlocks DELAY + LO-CUT, a tap
    relocks them. Off ALIGN a hold shows HINT_MUTE (B2 was the mute button until D-013)."""
    if page == "ALIGN":
        if kind == "long":
            return [Unlock(UNLOCK_S)]
        if kind == "short":
            return [Lock()] if focus.unlocked(now) else [ShowHint(HINT_LOCKED)]
        return []
    if kind == "long":                 # B2 was MUTE until D-013: a hold says where mute went
        return [ShowHint(HINT_MUTE)]
    if page == "SHOW":
        return [SelectBand(1)] if kind == "short" else []
    return [ShowHint(HINT_B2_OFF_SHOW)] if kind == "short" else []


def _setup_item(focus: Focus) -> SetupItem:
    return SETUP_ITEMS[max(0, min(len(SETUP_ITEMS) - 1, focus.item))]


def setup_value_of(state: Any, item: SetupItem) -> Any:
    """The current value of a SETUP item's field in `state` (None when unknown)."""
    get = getattr(state, "get", None)
    if not callable(get):
        return None
    try:
        v = get(item.field)
    except Exception:   # noqa: BLE001 - an odd state reads as unknown, never breaks a knob
        return None
    return bool(v) if item.kind == "bool" and v is not None else v


def setup_popup(focus: Any, now: float | None = None) -> tuple[str, Any] | None:
    """D-017: the SETUP popup open on the selected row, else None. ("pick", proposed value)
    while a risky row's proposal waits for its E3 push; ("confirm", None) while an action waits
    for its second E3 push. Never on a row the Controller holds read-only (its push only says
    why), never for a row other than the selected one. Tolerant of an odd focus (pages draws
    it)."""
    now = time.monotonic() if now is None else now
    try:
        item = SETUP_ITEMS[max(0, min(len(SETUP_ITEMS) - 1, int(getattr(focus, "item", 0) or 0)))]
        if item.field in (getattr(focus, "readonly", None) or {}):
            return None
        pick_now = getattr(focus, "pick_now", None)
        confirm_now = getattr(focus, "confirm_now", None)
        pk = pick_now(now) if callable(pick_now) else None
        if pk is not None and pk[0] == item.field and item.risky:
            return ("pick", pk[1])
        if callable(confirm_now) and confirm_now(now) == item.field and item.kind == "action":
            return ("confirm", None)
    except Exception:   # noqa: BLE001 - a broken focus shows no popup, never breaks a frame
        return None
    return None


def _choice_step(item: SetupItem, base: Any, delta: int) -> Any:
    """One choice from `base` in `delta`'s direction, stopping at both ends (D-017: a knob turned
    right never wraps back to the first choice, so a fast turn lands where it points). An ON/off
    is the direction itself: right = ON (INVERTED), left = off (normal). One step per event,
    however many detents it carries."""
    if item.kind == "bool":
        return delta > 0
    choices = item.choices
    if not choices:
        return base
    step = 1 if delta > 0 else -1
    i = choices.index(base) if base in choices else (-1 if step > 0 else len(choices))
    return choices[max(0, min(len(choices) - 1, i + step))]


def _already(item: SetupItem, value: Any) -> ShowHint:
    return ShowHint(HINT_ALREADY.format(choice_name(item, value).upper()))


def _setup_apply_step(focus: Focus, item: SetupItem, delta: int, state: Any) -> list[Action]:
    """E3 on an everyday ON/off or choice (not risky): one step in the turn's direction, set at
    once through the controller's SetChoice path; already there = a hint, nothing sent."""
    cur = setup_value_of(state, item)
    target = _choice_step(item, cur, delta)
    focus.clear_pick()
    focus.confirm = None
    if target == cur:
        return [_already(item, cur)]
    return [SetChoice(item.field, target)]


def _setup_propose(focus: Focus, item: SetupItem, delta: int, now: float,
                   state: Any) -> list[Action]:
    """E3 on a risky row: open (or move) the popup's proposal one step in the turn's direction,
    from the proposal already shown, else from the current value. Back on the current value =
    the popup closes; no popup and already there = a hint. Never sets anything."""
    cur = setup_value_of(state, item)
    pk = focus.pick_now(now)
    open_ = pk is not None and pk[0] == item.field
    cand = _choice_step(item, pk[1] if open_ else cur, delta)
    focus.confirm = None
    if cand == cur:
        focus.clear_pick()
        return [] if open_ else [_already(item, cur)]
    focus.pick, focus.pick_value, focus.pick_until = item.field, cand, now + PICK_WINDOW_S
    focus.pick_at = now
    return []


def _setup_turn(focus: Focus, delta: int, fine: bool, now: float = 0.0,
                state: Any = None) -> list[Action]:
    """E3 turn on SETUP (D-017): a number moves (fine while E3's push is held), an everyday
    ON/off or choice steps and applies, a risky row proposes (the popup), a page or an action
    says to push E3."""
    item = _setup_item(focus)
    ro = focus.readonly.get(item.field)
    if ro:              # a number too (D-016: the AUX level where no input is spare)
        return [ShowHint(ro)]
    if item.kind == "num":
        return [Nudge(item.field, delta, fine)]
    if item.picks:
        if item.risky:
            return _setup_propose(focus, item, delta, now, state)
        return _setup_apply_step(focus, item, delta, state)
    if item.kind == "page":
        return [ShowHint(HINT_PUSH_OPEN)]
    waiting = focus.confirm_now(now) == item.field          # the popup: the next push runs it
    twice = (item.needs_confirm and item.field not in getattr(focus, "no_confirm", ())
             and not waiting)
    return [ShowHint(HINT_PUSH_TWICE if twice else HINT_PUSH_RUN)]


def _setup_e3_hint(focus: Focus, now: float) -> str:
    """What an E1 or E2 push on SETUP says (D-017: they act on nothing there): the E3 gesture
    the selected row takes, or why it is fixed."""
    item = _setup_item(focus)
    ro = focus.readonly.get(item.field)
    if ro:
        return ro
    pop = setup_popup(focus, now)
    if pop is not None:
        return HINT_PUSH_APPLY if pop[0] == "pick" else HINT_PUSH_RUN
    if item.kind in ("num", "bool", "choice"):
        return HINT_TURN_VALUE
    if item.kind == "page":
        return HINT_PUSH_OPEN
    twice = item.needs_confirm and item.field not in getattr(focus, "no_confirm", ())
    return HINT_PUSH_TWICE if twice else HINT_PUSH_RUN


def _setup_push(focus: Focus, now: float, state: Any = None) -> list[Action]:
    """E3 push on SETUP (D-017): apply a risky row's proposal (the popup), open a page, run an
    action. A confirmed action's first push opens its popup and the second, within
    CONFIRM_WINDOW_S, runs it (Laptop access while open: one push, `Focus.no_confirm`). On a
    number or an ON/off with nothing proposed it only says to turn E3."""
    item = _setup_item(focus)
    if item.kind == "page":
        return [GotoPage(item.field)]
    ro = focus.readonly.get(item.field)
    if ro:
        return [ShowHint(ro)]
    if item.picks:
        pk = focus.pick_now(now)
        if item.risky and pk is not None and pk[0] == item.field:
            pressed = focus.pressed_at.get(SETUP_VALUE_KNOB)
            if pressed is not None and pressed - focus.pick_at < PICK_SETTLE_S:
                # The knob clicked a detent as the finger pressed it (or a turn and a push came
                # as one gesture): the popup was never seen. It stays open; the next push applies.
                return [ShowHint(HINT_PUSH_APPLY)]
            focus.clear_pick()
            return [SetChoice(item.field, pk[1])]
        return [ShowHint(HINT_TURN_VALUE)]
    if item.kind == "num":
        return [ShowHint(HINT_TURN_VALUE)]
    if item.needs_confirm and item.field not in getattr(focus, "no_confirm", ()):
        if focus.confirm_now(now) != item.field:
            focus.confirm, focus.confirm_until = item.field, now + CONFIRM_WINDOW_S
            focus.clear_pick()
            return []                   # the popup asks; the next E3 push runs it
        focus.confirm = None
    if item.kind == "action":
        if item.field == A_RESET_LIVE:
            return [ResetLive()]
        if item.field == A_UNDO_RESET:
            return [UndoReset()]
        return [SetupAction(item.field)]
    return []


def _b2(focus: Focus, kind: str, muted: Any, mute_seq: Any) -> list[Action]:
    """The MUTE button, B4 since D-013 (the name `_b2` is kept from D-009: B2 was mute then).
    'press' records what the operator sees; 'long' (0.8 s later, still held) acts on it.
    Started on an unmuted box: always MUTE (a phone muting meanwhile must not turn it into an
    unmute). Started on a muted box: UNMUTE with the generation seen, which the controller
    refuses if the mute changed since. No press seen (an older event source): as before."""
    if kind == "press":
        focus.b2_seen = (muted, mute_seq)
        return []
    if kind == "long":
        seen, focus.b2_seen = focus.b2_seen, None
        if seen is None or seen[0] is None:
            return [mute_action(muted)]
        if not seen[0]:
            return [Mute(True)]
        return [Mute(False, seen[1] if isinstance(seen[1], int) else None)]
    if kind in ("release", "short"):
        focus.b2_seen = None
    return [ShowHint(HINT_MUTE)] if kind == "short" else []


def _hwtest(focus: Focus, event: Any, cmap: ControlMap, muted: Any = None,
            mute_seq: Any = None) -> list[Action]:
    """Record raw activity. B4 long still mutes (the safety gesture works on every page);
    a long press on any OTHER switch leaves the test (back to SETUP)."""
    hw = focus.hw
    hw.refresh_roles(cmap)
    idx = int(event.index)
    if _is_encoder(event):
        if 0 <= idx < 4:
            hw.enc_count[idx] += int(event.delta)
            hw.last = f"enc{idx} {int(event.delta):+d}  = {hw.enc_role[idx]}"
        else:
            hw.last = f"enc{idx} {int(event.delta):+d}  = ?"
        hw.lines.append(hw.last)
        return []
    kind = getattr(event, "kind", "")
    if 0 <= idx < 8:
        if kind == "press":
            hw.sw_down[idx] = True
        elif kind in ("release", "short"):
            hw.sw_down[idx] = False
        if kind in ("short", "long"):
            hw.sw_last[idx] = kind
    name = hw.sw_role[idx] if 0 <= idx < 8 else "?"
    hw.last = f"sw{idx} {kind}  = {name}"
    if kind in ("press", "short", "long"):   # keep the log readable: no 'release' lines
        hw.lines.append(hw.last)
    if cmap.switch_role(idx) == ("B", 3):          # B4 mutes on every page, as everywhere
        acts = _b2(focus, kind, muted, mute_seq)
        return [a for a in acts if isinstance(a, Mute)]
    if kind == "long":
        return [GotoPage("SETUP")]
    return []


def knob_labels(page: str) -> Sequence[str]:
    """E1..E4 labels for `page` (pages.py draws them at KNOB_CORNERS)."""
    return [k.label for k in KNOBS.get(page_name(page), KNOBS["HWTEST"])]
