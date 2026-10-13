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

Pages (D-009): SHOW (front) -> ALIGN -> SETUP on B1, plus PHONE, VENUES and HWTEST reached from
SETUP.
- SHOW: E1 MASTER, E2/E3/E4 = FREQ/GAIN/Q of the selected live band. B3 tap = next band in
  LS 1 2 3 4 HS order (wraps). B4 tap = the notch helper (Controller.notch_helper). An encoder
  push is FINE while held, never a select.
- ALIGN: E1 MASTER and E2 BALANCE live; E3 SUB DELAY and E4 LO-CUT are LOCKED: a turn shows
  HINT_LOCKED and changes nothing. A B3 hold unlocks them for UNLOCK_S, renewed by every input
  on ALIGN (so any UNLOCK_S of idle relocks); a B3 tap relocks at once; leaving ALIGN relocks.
- SETUP: E1 selects the item, E2 turns its value, an E1/E2 push selects it. Reset live EQ,
  Lo-Cut on/off and the pipeline rebuilds need a second push within CONFIRM_WINDOW_S.
- VENUES (SETUP > Venues, docs/12-venue-snapshots.md): E1 or E2 scrolls the 8 slots (and the
  Undo recall row), a push opens the slot: RECALL / SAVE HERE / RENAME / BACK. Recall, save and
  Undo recall need a second push within CONFIRM_WINDOW_S (the hint names the slot; an overwrite
  says so). A slot's confirm is bound to its saved time: saved again from the phone between the
  two pushes, the second push only re-arms. RENAME is a letter picker opened just after the
  name: E1 moves the cursor (16 cells, then OK, CLEAR, CANCEL), E2 changes the letter, a push
  steps to the next cell; a push on a blank cell right after another blank jumps to OK.
- B1: press = next page (instant), hold = SHOW from anywhere. The Controller brings ALIGN, SETUP
  and VENUES back to SHOW after HOME_AFTER_S without input.

Safety rules carried here:
- B2 mutes/unmutes on a LONG press only, on EVERY page, the Knob test included; a short press
  only shows a hint. B2 emits an explicit `Mute(bool)` when the dispatcher passes the mute the
  screen shows (`map_event(..., muted=...)`): the operator presses for the opposite of what
  they see, never for a state the controller has not adopted yet.
- Resetting the live EQ is a SETUP item with a confirm (and a one-step undo in the Controller):
  B4 on SHOW only ever selects a band and sets an inaudible one up (gain is never touched).
- SUB DELAY (every settled change dips the subs under the delay guard) keeps v0's wake-up
  detent on top of the lock: the first detent after ARM_WINDOW_S at rest only wakes it.
- Pipeline-rebuilding settings (mono subs, audition, profile) need two pushes on SETUP.
- A venue recall (many values at once) and its undo need two pushes, on the slot it names (as it
  was when the first push armed); no button recalls.
- Acceleration makes master go DOWN faster; it never accelerates master UP (and the Controller
  caps master rises per window: the master-up guard).
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Literal, Sequence

from ..venues import N_SLOTS as VENUE_SLOTS
from ..venues import NAME_MAX as VENUE_NAME_MAX
from ..venues import NAME_PICKER as VENUE_PICKER

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
class SelectSlot:
    """Move the selected live-EQ slot by `delta` in LIVE_SLOTS order (UI-local, wraps). Kept
    for v0 callers; the knobs use SelectBand."""
    delta: int


@dataclass(frozen=True)
class SelectBand:
    """SHOW, B3 tap: the next band in BAND_CYCLE order (LS 1 2 3 4 HS, wraps). UI-local."""
    delta: int = 1


@dataclass(frozen=True)
class SelectItem:
    """SETUP page: move the selected item (UI-local, clamps). Contract addition."""
    delta: int


@dataclass(frozen=True)
class NextPage:
    pass


@dataclass(frozen=True)
class GotoPage:
    """Jump to a named page (B1 hold -> SHOW, SETUP -> HWTEST/PHONE and back)."""
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
    """B2 long: the opposite of the mute the screen shows; 'toggle' only if that is unknown.

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
class RecallVenue:
    """VENUES > slot > RECALL, confirmed: the slot's values through the Controller's parameter
    writes (Controller.recall_venue; never master or mute). `saved_at`: the slot's saved time
    when the confirm was armed (refused by the Controller if it was saved again since)."""
    slot: int
    saved_at: float | None = None


@dataclass(frozen=True)
class SaveVenue:
    """VENUES > slot > SAVE HERE, confirmed: the current venue values into the slot (its name
    kept; an empty slot gets VENUE n)."""
    slot: int


@dataclass(frozen=True)
class RenameVenue:
    """VENUES > slot > RENAME > OK: the slot's new name (sanitised by the Controller)."""
    slot: int
    name: str


@dataclass(frozen=True)
class UndoRecall:
    """VENUES > Undo recall, confirmed: the values from before the last recall
    (Controller.undo_recall)."""


@dataclass(frozen=True)
class NotchHelper:
    """SHOW, B4 tap: set a free bell up as a notch (Controller.notch_helper; gain untouched)."""


@dataclass(frozen=True)
class Unlock:
    """ALIGN, B3 hold: E3/E4 (sub delay, Lo-Cut) editable until `seconds` without input on
    ALIGN. UI-local (Focus.unlock_until)."""
    seconds: float = 30.0          # UNLOCK_S


@dataclass(frozen=True)
class Lock:
    """ALIGN, B3 tap while unlocked: E3/E4 locked again at once. UI-local."""


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


Action = (Nudge | Toggle | SelectSlot | SelectBand | SelectItem | NextPage | GotoPage | Mute
          | ResetLive | UndoReset | SetupAction | NotchHelper | Unlock | Lock | ShowHint
          | PhoneToggle | PhoneQr | RecallVenue | SaveVenue | RenameVenue | UndoRecall)

# ------------------------------------------------------------------------------------------
# Field names, slots, pages, SETUP items (shared with pages.py)
# ------------------------------------------------------------------------------------------
# Field names are k1000.state field paths ("master_db", "live_peq.2.gain", "out_trim_db.1"), so
# the controller can apply a Nudge with `state.nudge(field, detents, fine)` and a Toggle by
# flipping the bool (or stepping through state.CHOICES for an enum field).
F_MASTER = "master_db"         # Main fader
F_BALANCE = "balance_db"       # macro: Aux1 = +b/2, Aux2 = -b/2
F_SUB_DELAY = "sub_delay_ms"
F_LOWCUT_FREQ = "lowcut_hz"    # E4 on ALIGN
F_LOWCUT_ON = "lowcut_on"      # SETUP > Lo-Cut (confirmed)
F_SPECTRUM_ON = "spectrum_on"  # SETUP > Spectrum
F_SLOT = "slot"                # pseudo-field: highlight when the selected band changes

LIVE_SLOTS: tuple[str, ...] = ("live_peq.0", "live_peq.1", "live_peq.2", "live_peq.3",
                               "live_lowshelf", "live_highshelf")
LIVE_SLOT_LABELS: tuple[str, ...] = ("PEQ 1", "PEQ 2", "PEQ 3", "PEQ 4", "LO SHELF", "HI SHELF")
LIVE_SLOT_SHORT: tuple[str, ...] = ("P1", "P2", "P3", "P4", "LS", "HS")
# B3's band order on SHOW, low to high as drawn: LS 1 2 3 4 HS (LIVE_SLOTS indices).
BAND_CYCLE: tuple[int, ...] = (4, 0, 1, 2, 3, 5)
BELL_SLOTS: tuple[int, ...] = (0, 1, 2, 3)      # the notch helper's candidates (never a shelf)

PAGES: tuple[str, ...] = ("SHOW", "ALIGN", "SETUP")   # B1 cycles these; HWTEST/PHONE via SETUP
ALL_PAGES: tuple[str, ...] = PAGES + ("HWTEST", "PHONE", "VENUES")   # via SETUP (D-007, docs/12)
HOME_PAGE = "SHOW"
# v0 page names, still accepted where a page is named (console `page MAIN`, old callers).
PAGE_ALIASES: dict[str, str] = {"MAIN": "ALIGN", "LIVE": "SHOW"}
AUTO_HOME_PAGES: tuple[str, ...] = ("ALIGN", "SETUP", "VENUES")
HOME_AFTER_S = 60.0           # ALIGN/SETUP -> SHOW after this long without a knob/button event
UNLOCK_S = 30.0               # ALIGN E3/E4 unlock, renewed by every input on ALIGN


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
    """One SETUP row. kind: num (E2 turns it), bool (a push flips it), rebuild (a push steps
    it; pipeline rebuild), page (a push opens it), action (a push runs it: `field` names the
    action). `confirm`: the push must come twice within CONFIRM_WINDOW_S (rebuilds always)."""
    label: str
    field: str
    kind: Literal["num", "bool", "rebuild", "page", "action"]
    confirm: bool = False

    @property
    def needs_confirm(self) -> bool:
        return self.confirm or self.kind == "rebuild"


A_RESET_LIVE = "reset_live"     # SETUP action items (`SetupItem.field`)
A_UNDO_RESET = "undo_reset"
A_LAPTOP = "laptop_access"      # the Friday tune's laptop over the K1000 Wi-Fi (docs/06 §11)

# v0's list (Top delay stays here), then the v1 items (D-009), Venues (docs/12), Laptop access
# (opened with a double push, closed with one: the Controller waives its confirm while it is
# open), then Phone and the Knob test.
# Rebuild items (state.REBUILD_FIELDS) change the pipeline: possible click -> two pushes.
SETUP_ITEMS: tuple[SetupItem, ...] = (
    SetupItem("Input trim", "input_trim_db", "num"),
    SetupItem("Sub polarity", "sub_inverted", "bool"),
    SetupItem("Top polarity", "top_inverted", "bool"),
    SetupItem("Top delay", "top_delay_ms", "num"),
    SetupItem("Sub low-pass", "sub_lowpass_on", "bool"),
    SetupItem("Sub LP freq", "sub_lowpass_hz", "num"),
    SetupItem("Trim Sub L", "out_trim_db.0", "num"),
    SetupItem("Trim Sub R", "out_trim_db.1", "num"),
    SetupItem("Trim Top L", "out_trim_db.2", "num"),
    SetupItem("Trim Top R", "out_trim_db.3", "num"),
    SetupItem("Mono subs", "mono_subs", "rebuild"),
    SetupItem("Audition (dev)", "dev_audition", "rebuild"),
    SetupItem("Profile", "profile", "rebuild"),
    SetupItem("Reset live EQ", A_RESET_LIVE, "action", confirm=True),
    SetupItem("Undo reset", A_UNDO_RESET, "action"),
    SetupItem("Lo-Cut", F_LOWCUT_ON, "bool", confirm=True),
    SetupItem("Spectrum", F_SPECTRUM_ON, "bool"),
    SetupItem("Venues", "VENUES", "page"),        # 8 venue snapshots (docs/12)
    SetupItem("Laptop access", A_LAPTOP, "action", confirm=True),
    SetupItem("Phone", "PHONE", "page"),          # QR codes + phone on/off (D-007)
    SetupItem("Knob test", "HWTEST", "page"),
)


@dataclass(frozen=True)
class Knob:
    """One encoder's meaning on one page. `field` may contain '{slot}' (the selected band)."""
    label: str
    field: str | None                 # None = unused on this page
    accel_up: int = 1                 # max detent multiplier on fast turns, per direction
    accel_down: int = 1
    kind: Literal["nudge", "slot", "item", "setup_value", "phone_qr", "venue"] = "nudge"
    # The first detent after ARM_WINDOW_S of rest only wakes the knob (highlight + hint); the
    # next ones edit. For a value whose every change is audible as a dip (sub delay).
    arm: bool = False
    # ALIGN alignment knobs: a turn does nothing (HINT_LOCKED) until a B3 hold unlocks them.
    locked: bool = False


# E1..E4 per page — the contract's knob table, as data.
KNOBS: dict[str, tuple[Knob, Knob, Knob, Knob]] = {
    "SHOW": (
        Knob("MASTER", F_MASTER, accel_up=1, accel_down=4),
        Knob("FREQ", "{slot}.freq", accel_up=4, accel_down=4),
        Knob("GAIN", "{slot}.gain"),
        Knob("Q", "{slot}.q"),
    ),
    "ALIGN": (
        Knob("MASTER", F_MASTER, accel_up=1, accel_down=4),
        Knob("BALANCE", F_BALANCE),
        Knob("SUB DELAY", F_SUB_DELAY, arm=True, locked=True),
        Knob("LO-CUT", F_LOWCUT_FREQ, accel_up=4, accel_down=4, locked=True),
    ),
    "SETUP": (
        Knob("ITEM", None, kind="item"),
        Knob("VALUE", None, kind="setup_value"),
        Knob("", None),
        Knob("", None),
    ),
    "HWTEST": (Knob("", None), Knob("", None), Knob("", None), Knob("", None)),
    "PHONE": (Knob("QR", None, kind="phone_qr"), Knob("", None), Knob("", None), Knob("", None)),
    "VENUES": (Knob("SLOT", None, kind="venue"), Knob("SLOT", None, kind="venue"), Knob("", None),
               Knob("", None)),
}

# VENUES (docs/12): the list rows are the 8 slots, then Undo recall; an open slot's menu.
VENUE_UNDO_ROW = VENUE_SLOTS
VENUE_MENU: tuple[str, ...] = ("RECALL", "SAVE HERE", "RENAME", "BACK")
VENUE_OK = VENUE_NAME_MAX               # RENAME: cursor cells 0..15 = letters, then OK, CLEAR, CANCEL
VENUE_CLEAR = VENUE_NAME_MAX + 1
VENUE_CANCEL = VENUE_NAME_MAX + 2

# Screen corner of each logical encoder. Assumed (E1 top-left, E2 bottom-left, E3 top-right,
# E4 bottom-right) until Thomas confirms on the box — then correct this table only.
KNOB_CORNERS: tuple[str, str, str, str] = ("tl", "bl", "tr", "br")

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

HINT_MUTE = "HOLD B2 TO MUTE / UNMUTE"
HINT_LOCKED = "LOCKED · HOLD B3 TO EDIT"
HINT_UNLOCKED = "SUB DELAY + LO-CUT UNLOCKED"
HINT_RELOCKED = "SUB DELAY + LO-CUT LOCKED"
HINT_B3_OFF_SHOW = "B3: NEXT BAND ON SHOW"
HINT_B4_OFF_SHOW = "B4: NOTCH HELPER ON SHOW"
HINT_RESET_IN_SETUP = "RESET LIVE EQ: IN SETUP"
HINT_PUSH_TOGGLE = "PUSH TO CHANGE"
HINT_CONFIRM = "PUSH AGAIN TO APPLY (MAY CLICK)"
HINT_CONFIRM_RESET = "PUSH AGAIN TO RESET ALL LIVE EQ"
HINT_CONFIRM_LOWCUT = "PUSH AGAIN TO SWITCH THE LO-CUT"
HINT_CONFIRM_LAPTOP = "PUSH AGAIN: LAPTOP ACCESS FOR 2 H"
HINT_ARM = "{} · TURN AGAIN TO CHANGE"
HINT_VENUE_RECALL = "PUSH AGAIN TO RECALL {}"
HINT_VENUE_SAVE = "PUSH AGAIN TO SAVE IN SLOT {}"
HINT_VENUE_OVERWRITE = "PUSH AGAIN TO OVERWRITE {}"
HINT_VENUE_EMPTY = "SLOT EMPTY · SAVE HERE FIRST"
HINT_VENUE_RENAME = "E1 MOVES · E2 LETTER · OR NAME IT FROM THE PHONE"
HINT_VENUE_NO_NAME = "NAME IS EMPTY"
HINT_VENUE_CANCELLED = "RENAME CANCELLED"
HINT_VENUE_CLEARED = "NAME CLEARED · E2 PICKS THE FIRST LETTER"
HINT_VENUE_UNDO = "PUSH AGAIN TO UNDO THE RECALL"
HINT_VENUE_NO_UNDO = "NO RECALL TO UNDO"
CONFIRM_WINDOW_S = 5.0

# ------------------------------------------------------------------------------------------
# Physical -> logical mapping (from /etc/k1000/k1000.yaml, section `controls:`)
# ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ControlMap:
    """encoders[i] = physical encoder index of E(i+1); buttons[i] = switch index of B(i+1);
    pushes[i] = switch index of E(i+1)'s push; reverse[i] flips E(i+1)'s direction."""
    encoders: tuple[int, int, int, int] = (0, 1, 2, 3)
    buttons: tuple[int, int, int, int] = (4, 5, 6, 7)
    pushes: tuple[int, int, int, int] = (0, 1, 2, 3)
    reverse: tuple[bool, bool, bool, bool] = (False, False, False, False)

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
        return cls(enc, btn, psh, rev)

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
    confirm: str | None = None            # SETUP field/action waiting for its second push
    confirm_until: float = 0.0
    hw: HwTestLog = field(default_factory=HwTestLog)
    # B2 press: (the mute B2 acts against, its generation) as the operator saw them when the
    # press STARTED. B2 long fires 0.8 s later, while still held: it acts on that intent.
    b2_seen: tuple[Any, Any] | None = None
    # ALIGN: E3/E4 editable until this deadline (time.monotonic()); 0 = locked.
    unlock_until: float = 0.0
    # SETUP items whose confirm is waived right now (set by the Controller): Laptop access
    # while it is open, so that closing it, the safe direction, is a single push.
    no_confirm: set[str] = field(default_factory=set)
    # VENUES (docs/12): the list row (0..7 = slots, VENUE_UNDO_ROW = Undo recall), the mode
    # (list | menu: a slot is open | rename: the letter picker), the menu row, the rename buffer
    # and cursor. Kept up to date by the Controller: `venue_names` (name or None per slot),
    # `venue_stamps` (saved time or None per slot: binds a confirm to what was shown) and
    # `venue_undo_ok` (a recall can be undone).
    venue: int = 0
    venue_mode: str = "list"
    venue_menu: int = 0
    venue_buf: list[str] = field(default_factory=list)
    venue_cursor: int = 0
    venue_names: tuple[str | None, ...] = ()
    venue_stamps: tuple[float | None, ...] = ()
    venue_undo_ok: bool = False

    def venue_name(self, slot: int) -> str | None:
        """The name of `slot` as the Controller last said (None = empty or unknown)."""
        try:
            n = self.venue_names[slot]
        except (IndexError, TypeError):
            return None
        return str(n) if n else None

    def editing_now(self, now: float | None = None) -> str | None:
        now = time.monotonic() if now is None else now
        return self.editing if self.editing and now < self.edit_until else None

    def hint_now(self, now: float | None = None) -> str | None:
        now = time.monotonic() if now is None else now
        return self.hint if self.hint and now < self.hint_until else None

    def confirm_now(self, now: float | None = None) -> str | None:
        now = time.monotonic() if now is None else now
        return self.confirm if self.confirm and now < self.confirm_until else None

    def unlocked(self, now: float | None = None) -> bool:
        """ALIGN's E3/E4 (sub delay, Lo-Cut) are editable now."""
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
    if isinstance(action, (Nudge, Toggle)):
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
              muted: bool | None = None, mute_seq: int | None = None) -> list[Action]:
    """Map one hardware event to actions for `page` (v0 names MAIN/LIVE are aliases).

    `event` is duck-typed: an encoder event has `.index`, `.delta` (signed detents) and
    optionally `.ts` (monotonic seconds); a switch event has `.index` and `.kind` in
    {'press', 'release', 'short', 'long'}. `cfg` is the k1000.yaml dict (or a ControlMap).
    `muted` is the mute the screen currently shows (`pages.displayed_muted(state, status)`):
    B2 long then emits `Mute(not muted)`; with None it falls back to `Mute('toggle')`.
    `mute_seq` is its generation (Controller.mute_view). Both are recorded at the B2 PRESS: the
    long press acts on what the operator meant when the finger went down (a mute stays a mute
    whatever a phone did meanwhile; an unmute carries `seen` and is refused if the mute changed).
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
        # Slot/item: every detent counts (the driver merges a fast turn into one event).
        if knob.kind == "slot":
            return [SelectSlot(delta)]
        if knob.kind == "item":
            return [SelectItem(delta)]
        if knob.kind == "setup_value":
            return _setup_turn(focus, delta, fine)
        if knob.kind == "phone_qr":
            return [PhoneQr(1 if delta > 0 else -1)]
        if knob.kind == "venue":
            return _venue_turn(focus, e, delta)
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
        elif kind in ("release", "short"):
            focus.held.discard(n)
            focus.turned.discard(n)
        # A push released after turning its knob (the fine-mode habit from SHOW) is not a
        # 'select': on SETUP it would flip a polarity or open a page.
        if page == "PHONE" and kind == "short" and n in (0, 1) and not turned:
            return [PhoneToggle()]
        if page == "SETUP" and kind == "short" and n in (0, 1) and not turned:
            return _setup_push(focus, now)
        if page == "VENUES" and kind == "short" and n in (0, 1) and not turned:
            return _venue_push(focus, now)
        return []

    if n == 0:   # B1: next page on press (navigation should feel instant); hold = SHOW
        if kind == "press":
            return [NextPage()]
        return [GotoPage(HOME_PAGE)] if kind == "long" else []
    if n == 1:   # B2 mute — long press only, on the intent of its press
        return _b2(focus, kind, muted, mute_seq)
    if n == 2:   # B3: next band (SHOW), unlock/lock the alignment knobs (ALIGN)
        return _b3(page, focus, kind, now)
    if n == 3:   # B4: the notch helper (SHOW)
        if page == "SHOW":
            if kind == "short":
                return [NotchHelper()]
            return [ShowHint(HINT_RESET_IN_SETUP)] if kind == "long" else []
        return [ShowHint(HINT_B4_OFF_SHOW)] if kind == "short" else []
    return []


def _b3(page: str, focus: Focus, kind: str, now: float) -> list[Action]:
    if page == "SHOW":
        return [SelectBand(1)] if kind == "short" else []
    if page == "ALIGN":
        if kind == "long":
            return [Unlock(UNLOCK_S)]
        if kind == "short":
            return [Lock()] if focus.unlocked(now) else [ShowHint(HINT_LOCKED)]
        return []
    return [ShowHint(HINT_B3_OFF_SHOW)] if kind == "short" else []


def _setup_item(focus: Focus) -> SetupItem:
    return SETUP_ITEMS[max(0, min(len(SETUP_ITEMS) - 1, focus.item))]


def _setup_turn(focus: Focus, delta: int, fine: bool) -> list[Action]:
    item = _setup_item(focus)
    if item.kind == "num":
        return [Nudge(item.field, delta, fine)]
    return [ShowHint(HINT_PUSH_TOGGLE)]


def _confirm_hint(item: SetupItem) -> str:
    if item.field == A_RESET_LIVE:
        return HINT_CONFIRM_RESET
    if item.field == F_LOWCUT_ON:
        return HINT_CONFIRM_LOWCUT
    if item.field == A_LAPTOP:
        return HINT_CONFIRM_LAPTOP
    return HINT_CONFIRM


def _setup_push(focus: Focus, now: float) -> list[Action]:
    """E1/E2 push on SETUP = 'select' the current item (twice when it needs a confirm)."""
    item = _setup_item(focus)
    if item.kind == "page":
        return [GotoPage(item.field)]
    if item.needs_confirm and item.field not in getattr(focus, "no_confirm", ()):
        if focus.confirm_now(now) != item.field:
            focus.confirm, focus.confirm_until = item.field, now + CONFIRM_WINDOW_S
            return [ShowHint(_confirm_hint(item))]
        focus.confirm = None
    if item.kind in ("bool", "rebuild"):
        return [Toggle(item.field)]
    if item.kind == "action":
        if item.field == A_RESET_LIVE:
            return [ResetLive()]
        if item.field == A_UNDO_RESET:
            return [UndoReset()]
        return [SetupAction(item.field)]
    return []


def _venue_slot(focus: Focus) -> int:
    try:
        return max(0, min(VENUE_UNDO_ROW, int(focus.venue)))
    except (TypeError, ValueError):
        return 0


def _venue_turn(focus: Focus, e: int, delta: int) -> list[Action]:
    """VENUES, E1/E2 turn. List: E1 or E2 scrolls the slots (and Undo recall). Menu: E1 or E2
    picks the action. Rename: E1 moves the cursor, E2 changes the letter under it. Every detent
    counts; any move cancels a pending confirm. UI-local: nothing reaches the Controller."""
    focus.confirm = None
    mode = focus.venue_mode
    if mode == "menu":
        focus.venue_menu = max(0, min(len(VENUE_MENU) - 1, int(focus.venue_menu) + delta))
        return []
    if mode == "rename":
        if e == 0:
            focus.venue_cursor = max(0, min(VENUE_CANCEL, int(focus.venue_cursor) + delta))
        elif e == 1 and 0 <= focus.venue_cursor < VENUE_NAME_MAX:
            buf = _venue_buffer(focus)
            ch = buf[focus.venue_cursor]
            i = VENUE_PICKER.index(ch) if ch in VENUE_PICKER else 0
            buf[focus.venue_cursor] = VENUE_PICKER[(i + delta) % len(VENUE_PICKER)]
            focus.venue_buf = buf
        return []
    focus.venue_mode = "list"
    focus.venue = max(0, min(VENUE_UNDO_ROW, _venue_slot(focus) + delta))
    return []


def _venue_buffer(focus: Focus) -> list[str]:
    buf = [c if isinstance(c, str) and len(c) == 1 else " " for c in list(focus.venue_buf)]
    return (buf + [" "] * VENUE_NAME_MAX)[:VENUE_NAME_MAX]


def venue_stamp(focus: Any, slot: int) -> float | None:
    """The saved time of `slot` as the Controller last said (None = empty or unknown)."""
    try:
        at = getattr(focus, "venue_stamps", ())[slot]
    except (IndexError, TypeError, KeyError):
        return None
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return None
    return float(at)


def venue_confirm_key(focus: Any, act: str, slot: int) -> str:
    """The confirm key of a slot's RECALL ('recall') or SAVE HERE ('save'): the slot AND its
    saved time, so a slot saved again (from the phone) between the two pushes no longer matches
    and the second push re-arms on what is there now instead of acting on what the operator did
    not see. pages.py draws the pending row red by the same key."""
    at = venue_stamp(focus, slot)
    return f"venue_{act}:{slot}" + (f"@{at:.3f}" if at is not None else "")


VENUE_UNDO_KEY = "venue_undo"


def _venue_confirm(focus: Focus, key: str, now: float) -> bool:
    """The double push: True on the second push within CONFIRM_WINDOW_S (same key)."""
    if focus.confirm_now(now) != key:
        focus.confirm, focus.confirm_until = key, now + CONFIRM_WINDOW_S
        return False
    focus.confirm = None
    return True


def _venue_push(focus: Focus, now: float) -> list[Action]:
    """VENUES, E1/E2 push (not after a fine turn).

    List: a slot row opens it (on RECALL when it holds a venue, on SAVE HERE when empty); the
    Undo recall row needs a second push (a fast spin past slot 8 lands on it: one push must not
    revert delays, polarity and EQ). Menu: RECALL and SAVE HERE need a second push, bound to the
    slot's saved time (the hint names the slot, an overwrite says OVERWRITE); RENAME opens the
    picker just after the current name; BACK returns to the list. Rename: a push on a letter
    steps to the next cell, on a blank right after another blank jumps to OK (the name is done),
    on OK emits RenameVenue, on CLEAR blanks the name, on CANCEL returns to the menu."""
    slot = _venue_slot(focus)
    mode = focus.venue_mode
    if mode == "rename":
        cur = int(focus.venue_cursor)
        if cur == VENUE_CANCEL:
            focus.venue_mode = "menu"
            return [ShowHint(HINT_VENUE_CANCELLED)]
        if cur == VENUE_CLEAR:
            focus.venue_buf, focus.venue_cursor = [" "] * VENUE_NAME_MAX, 0
            return [ShowHint(HINT_VENUE_CLEARED)]
        if cur == VENUE_OK:
            name = "".join(_venue_buffer(focus)).strip()
            if not name:
                return [ShowHint(HINT_VENUE_NO_NAME)]
            focus.venue_mode = "menu"
            return [RenameVenue(slot, name)]
        buf = _venue_buffer(focus)
        cur = max(0, min(VENUE_NAME_MAX - 1, cur))
        if buf[cur] == " " and cur > 0 and buf[cur - 1] == " ":
            focus.venue_cursor = VENUE_OK         # two blanks in a row: the name is done
        else:
            focus.venue_cursor = min(VENUE_OK, cur + 1)
        return []
    if mode == "menu" and slot < VENUE_SLOTS:
        name = focus.venue_name(slot)
        act = VENUE_MENU[max(0, min(len(VENUE_MENU) - 1, int(focus.venue_menu)))]
        if act == "BACK":
            focus.venue_mode, focus.confirm = "list", None
            return []
        if act == "RECALL":
            if name is None:
                return [ShowHint(HINT_VENUE_EMPTY)]
            if not _venue_confirm(focus, venue_confirm_key(focus, "recall", slot), now):
                return [ShowHint(HINT_VENUE_RECALL.format(name))]
            focus.venue_mode = "list"
            return [RecallVenue(slot, venue_stamp(focus, slot))]
        if act == "SAVE HERE":
            if not _venue_confirm(focus, venue_confirm_key(focus, "save", slot), now):
                return [ShowHint(HINT_VENUE_OVERWRITE.format(name) if name is not None
                                 else HINT_VENUE_SAVE.format(slot + 1))]
            focus.venue_mode = "list"
            return [SaveVenue(slot)]
        # RENAME: the picker opens on the current name, the cursor just after it
        if name is None:
            return [ShowHint(HINT_VENUE_EMPTY)]
        focus.venue_mode = "rename"
        nm = name.upper()[:VENUE_NAME_MAX]
        focus.venue_buf = list(nm.ljust(VENUE_NAME_MAX))
        focus.venue_cursor = len(nm) if len(nm) < VENUE_NAME_MAX else VENUE_OK
        return [ShowHint(HINT_VENUE_RENAME, 4.0)]
    # list (or a menu left open on the Undo row: back to the list)
    focus.venue_mode = "list"
    if slot == VENUE_UNDO_ROW:
        if not focus.venue_undo_ok:
            focus.confirm = None
            return [ShowHint(HINT_VENUE_NO_UNDO)]
        if not _venue_confirm(focus, VENUE_UNDO_KEY, now):
            return [ShowHint(HINT_VENUE_UNDO)]
        return [UndoRecall()]
    focus.venue_mode = "menu"
    focus.venue_menu = 0 if focus.venue_name(slot) is not None else 1
    focus.confirm = None
    return []


def _b2(focus: Focus, kind: str, muted: Any, mute_seq: Any) -> list[Action]:
    """B2: 'press' records what the operator sees; 'long' (0.8 s later, still held) acts on it.
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
    """Record raw activity. B2 long still mutes (the safety gesture works on every page);
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
    if cmap.switch_role(idx) == ("B", 1):          # B2 mutes on every page, as everywhere
        acts = _b2(focus, kind, muted, mute_seq)
        return [a for a in acts if isinstance(a, Mute)]
    if kind == "long":
        return [GotoPage("SETUP")]
    return []


def knob_labels(page: str) -> Sequence[str]:
    """E1..E4 labels for `page` (pages.py draws them at KNOB_CORNERS)."""
    return [k.label for k in KNOBS.get(page_name(page), KNOBS["HWTEST"])]
