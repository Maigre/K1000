"""Knob and button semantics per page: hardware events -> UI actions.

Why a pure table: the physical position of every knob and button on the Zynthian V4 shell is
not confirmed yet, and the show-time meaning must be testable without hardware. So:

    physical event --(cfg 'controls' mapping)--> logical E1..E4 / B1..B4 / push P1..P4
                   --(KNOBS table, per page)----> Action objects for the controller

`map_event()` never touches audio. It only keeps the *input bookkeeping* of `Focus` (which
encoder pushes are held, when each knob last turned, a pending SETUP confirm, the HWTEST log)
and returns actions. The dispatcher then runs, for each action,
`apply_focus(focus, action)` (UI-local: slot/item selection, hints, the edit highlight) and,
when that returns False, hands the action to the Controller.

Safety rules carried here (contract §4, knob semantics):
- B2 mute and B4 reset-live act on a LONG press only; a short press only shows a hint.
- B2 long mutes on EVERY page, the Knob test included (there, the other switches' long press
  leaves the test instead).
- B4 reset-live acts on the LIVE page only and needs a second long press within
  CONFIRM_WINDOW_S: the same gesture as the B2 panic mute must never wipe the feedback notches
  by mistake. B3 (Lo-Cut) acts on MAIN only, where its value is shown.
- SUB DELAY on MAIN (every settled change dips the subs under the delay guard) wakes up on a
  first detent and changes from the next one: a brushed knob does not drop the subs.
- B2 emits an explicit `Mute(bool)` when the dispatcher passes the mute the screen shows
  (`map_event(..., muted=pages.displayed_muted(state, status))`): the operator presses for the
  opposite of what they see, never for a state the controller has not adopted yet.
- Pipeline-rebuilding settings (mono subs, audition) need two pushes on SETUP.
- Acceleration makes master go DOWN faster than it goes up.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Literal, Sequence

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
    """LIVE page: move the selected live-EQ slot (UI-local, wraps)."""
    delta: int


@dataclass(frozen=True)
class SelectItem:
    """SETUP page: move the selected item (UI-local, clamps). Contract addition."""
    delta: int


@dataclass(frozen=True)
class NextPage:
    pass


@dataclass(frozen=True)
class GotoPage:
    """Jump to a named page (SETUP -> HWTEST and back). Contract addition."""
    page: str


MUTE_TOGGLE = "toggle"


@dataclass(frozen=True)
class Mute:
    """on=True/False sets the mute; on='toggle' flips it (only emitted when map_event was not
    told the displayed mute — the dispatcher should always pass it)."""
    on: bool | Literal["toggle"] = MUTE_TOGGLE


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
    pass


@dataclass(frozen=True)
class ShowHint:
    text: str
    seconds: float = 2.5          # HINT_S


Action = Nudge | Toggle | SelectSlot | SelectItem | NextPage | GotoPage | Mute | ResetLive | ShowHint

# ------------------------------------------------------------------------------------------
# Field names, slots, pages, SETUP items (shared with pages.py)
# ------------------------------------------------------------------------------------------
# Field names are k1000.state field paths ("master_db", "live_peq.2.gain", "out_trim_db.1"), so
# the controller can apply a Nudge with `state.nudge(field, detents, fine)` and a Toggle by
# flipping the bool (or stepping through state.CHOICES for an enum field).
F_MASTER = "master_db"         # Main fader
F_BALANCE = "balance_db"       # macro: Aux1 = +b/2, Aux2 = -b/2
F_SUB_DELAY = "sub_delay_ms"
F_LOWCUT_FREQ = "lowcut_hz"    # E4 on MAIN
F_LOWCUT_ON = "lowcut_on"      # B3
F_SLOT = "slot"                # pseudo-field: highlight when the LIVE slot changes

LIVE_SLOTS: tuple[str, ...] = ("live_peq.0", "live_peq.1", "live_peq.2", "live_peq.3",
                               "live_lowshelf", "live_highshelf")
LIVE_SLOT_LABELS: tuple[str, ...] = ("PEQ 1", "PEQ 2", "PEQ 3", "PEQ 4", "LO SHELF", "HI SHELF")
LIVE_SLOT_SHORT: tuple[str, ...] = ("P1", "P2", "P3", "P4", "LS", "HS")

PAGES: tuple[str, ...] = ("MAIN", "LIVE", "SETUP")   # B1 cycles these; HWTEST via SETUP
ALL_PAGES: tuple[str, ...] = PAGES + ("HWTEST",)


def live_field(slot: int, param: str) -> str:
    """'live_peq.1.freq' etc. — slot index wraps into LIVE_SLOTS."""
    return f"{LIVE_SLOTS[slot % len(LIVE_SLOTS)]}.{param}"


@dataclass(frozen=True)
class SetupItem:
    label: str
    field: str
    kind: Literal["num", "bool", "rebuild", "page"]


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
    SetupItem("Knob test", "HWTEST", "page"),
)


@dataclass(frozen=True)
class Knob:
    """One encoder's meaning on one page. `field` may contain '{slot}' (LIVE)."""
    label: str
    field: str | None                 # None = unused on this page
    accel_up: int = 1                 # max detent multiplier on fast turns, per direction
    accel_down: int = 1
    kind: Literal["nudge", "slot", "item", "setup_value"] = "nudge"
    # The first detent after ARM_WINDOW_S of rest only wakes the knob (highlight + hint); the
    # next ones edit. For a value whose every change is audible as a dip (sub delay).
    arm: bool = False


# E1..E4 per page — the contract's knob table, as data.
KNOBS: dict[str, tuple[Knob, Knob, Knob, Knob]] = {
    "MAIN": (
        Knob("MASTER", F_MASTER, accel_up=2, accel_down=4),
        Knob("BALANCE", F_BALANCE),
        Knob("SUB DELAY", F_SUB_DELAY, arm=True),
        Knob("LO-CUT", F_LOWCUT_FREQ, accel_up=4, accel_down=4),
    ),
    "LIVE": (
        Knob("SLOT", F_SLOT, kind="slot"),
        Knob("FREQ", "{slot}.freq", accel_up=4, accel_down=4),
        Knob("GAIN", "{slot}.gain"),
        Knob("Q", "{slot}.q"),
    ),
    "SETUP": (
        Knob("ITEM", None, kind="item"),
        Knob("VALUE", None, kind="setup_value"),
        Knob("", None),
        Knob("", None),
    ),
    "HWTEST": (Knob("", None), Knob("", None), Knob("", None), Knob("", None)),
}

# Screen corner of each logical encoder. Assumed (E1 top-left, E2 bottom-left, E3 top-right,
# E4 bottom-right) until Thomas confirms on the box — then correct this table only.
KNOB_CORNERS: tuple[str, str, str, str] = ("tl", "bl", "tr", "br")

# Acceleration: turn rate (detents/s) -> multiplier, the same curve as the knob driver's
# (x1 up to 12 det/s, the knob's cap from 40 det/s). A direction change or a pause longer than
# the window resets to x1, so correcting an overshoot is always fine-grained. Applied HERE only
# (per-field caps); the controller must not accelerate again.
ACCEL_WINDOW_S = 0.25
ACCEL_LOW_DPS = 12.0
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
HINT_RESET = "HOLD B4 TO RESET LIVE EQ"
HINT_RESET_AGAIN = "HOLD B4 AGAIN TO RESET ALL LIVE EQ"
HINT_RESET_ON_LIVE = "RESET LIVE EQ: B4 ON THE LIVE PAGE"
HINT_LOWCUT_ON_MAIN = "LO-CUT: B3 ON THE MAIN PAGE"
HINT_PUSH_TOGGLE = "PUSH TO CHANGE"
HINT_CONFIRM = "PUSH AGAIN TO APPLY (MAY CLICK)"
HINT_ARM = "{} · TURN AGAIN TO CHANGE"
CONFIRM_WINDOW_S = 5.0
F_RESET_LIVE = "reset_live"    # pseudo-field: B4's pending confirm

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
    slot: int = 0                         # LIVE: index into LIVE_SLOTS
    item: int = 0                         # SETUP: index into SETUP_ITEMS
    editing: str | None = None            # field to highlight (last nudged/toggled)
    edit_until: float = 0.0               # time.monotonic() deadline of the highlight
    hint: str | None = None
    hint_until: float = 0.0
    held: set[int] = field(default_factory=set)          # logical encoders whose push is down
    turned: set[int] = field(default_factory=set)        # held pushes whose knob was turned
    last_turn: dict[int, tuple[float, int]] = field(default_factory=dict)  # enc -> (ts, sign)
    confirm: str | None = None            # SETUP field waiting for its second push
    confirm_until: float = 0.0
    hw: HwTestLog = field(default_factory=HwTestLog)

    def editing_now(self, now: float | None = None) -> str | None:
        now = time.monotonic() if now is None else now
        return self.editing if self.editing and now < self.edit_until else None

    def hint_now(self, now: float | None = None) -> str | None:
        now = time.monotonic() if now is None else now
        return self.hint if self.hint and now < self.hint_until else None

    def confirm_now(self, now: float | None = None) -> str | None:
        now = time.monotonic() if now is None else now
        return self.confirm if self.confirm and now < self.confirm_until else None


def apply_focus(focus: Focus, action: Action, now: float | None = None) -> bool:
    """Apply the UI-local part of `action` to `focus`. True = fully handled (do not forward).

    Nudge/Toggle also set the edit highlight but return False: the controller must act on them.
    """
    now = time.monotonic() if now is None else now
    if isinstance(action, SelectSlot):
        focus.slot = (focus.slot + action.delta) % len(LIVE_SLOTS)
        focus.editing, focus.edit_until = F_SLOT, now + EDIT_HIGHLIGHT_S
        return True
    if isinstance(action, SelectItem):
        focus.item = max(0, min(len(SETUP_ITEMS) - 1, focus.item + action.delta))
        focus.confirm = None
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
           fine: bool) -> int:
    """Scale `delta` by turn rate; x1 when fine, reversing, after a pause, or for flat knobs."""
    cap = knob.accel_up if delta > 0 else knob.accel_down
    sign = 1 if delta > 0 else -1
    if fine or cap <= 1 or last is None or last[1] != sign:
        return delta
    dt = now - last[0]
    if dt > ACCEL_WINDOW_S:
        return delta
    rate = abs(delta) / max(dt, 0.002)
    return delta * accel_multiplier(rate, ACCEL_LOW_DPS, ACCEL_HIGH_DPS, cap)


def map_event(page: str, focus: Focus, event: Any, cfg: Any = None, *,
              muted: bool | None = None) -> list[Action]:
    """Map one hardware event to actions for `page`.

    `event` is duck-typed: an encoder event has `.index`, `.delta` (signed detents) and
    optionally `.ts` (monotonic seconds); a switch event has `.index` and `.kind` in
    {'press', 'release', 'short', 'long'}. `cfg` is the k1000.yaml dict (or a ControlMap).
    `muted` is the mute the screen currently shows (`pages.displayed_muted(state, status)`):
    B2 long then emits `Mute(not muted)`; with None it falls back to `Mute('toggle')`.
    """
    cmap = ControlMap.from_cfg(cfg)
    now = getattr(event, "ts", None)
    now = time.monotonic() if now is None else float(now)

    if page == "HWTEST":
        return _hwtest(focus, event, cmap, muted)

    if _is_encoder(event):
        e = cmap.encoder_role(int(event.index))
        if e is None or not event.delta:
            return []
        delta = int(event.delta) * (-1 if cmap.reverse[e] else 1)
        knobs = KNOBS.get(page)
        if knobs is None:
            return []
        knob = knobs[e]
        last = focus.last_turn.get(e)
        focus.last_turn[e] = (now, 1 if delta > 0 else -1)
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
        if knob.field is None:
            return []
        fld = knob.field.replace("{slot}", LIVE_SLOTS[focus.slot % len(LIVE_SLOTS)])
        if knob.arm and not fine and (last is None or now - last[0] > ARM_WINDOW_S):
            focus.editing, focus.edit_until = fld, now + EDIT_HIGHLIGHT_S
            return [ShowHint(HINT_ARM.format(knob.label))]
        return [Nudge(fld, _accel(knob, delta, last, now, fine), fine)]

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
        # A push released after turning its knob (the fine-mode habit from LIVE) is not a
        # 'select': on SETUP it would flip a polarity or open a page.
        if page == "SETUP" and kind == "short" and n in (0, 1) and not turned:
            return _setup_push(focus, now)
        return []

    if n == 0:   # B1 next page — on press, navigation should feel instant
        return [NextPage()] if kind == "press" else []
    if n == 1:   # B2 mute — long press only
        if kind == "long":
            return [mute_action(muted)]
        return [ShowHint(HINT_MUTE)] if kind == "short" else []
    if n == 2:   # B3 Lo-Cut on/off — on a deliberate release, on MAIN (where it is shown)
        if kind != "short":
            return []
        return [Toggle(F_LOWCUT_ON)] if page == "MAIN" else [ShowHint(HINT_LOWCUT_ON_MAIN)]
    if n == 3:   # B4 reset live EQ — LIVE page, long press, confirmed by a second long press
        if page != "LIVE":
            return [ShowHint(HINT_RESET_ON_LIVE)] if kind in ("short", "long") else []
        if kind == "long":
            if focus.confirm_now(now) == F_RESET_LIVE:
                focus.confirm = None
                return [ResetLive()]
            focus.confirm, focus.confirm_until = F_RESET_LIVE, now + CONFIRM_WINDOW_S
            return [ShowHint(HINT_RESET_AGAIN, CONFIRM_WINDOW_S)]
        return [ShowHint(HINT_RESET)] if kind == "short" else []
    return []


def _setup_turn(focus: Focus, delta: int, fine: bool) -> list[Action]:
    item = SETUP_ITEMS[max(0, min(len(SETUP_ITEMS) - 1, focus.item))]
    if item.kind == "num":
        return [Nudge(item.field, delta, fine)]
    return [ShowHint(HINT_PUSH_TOGGLE)]


def _setup_push(focus: Focus, now: float) -> list[Action]:
    """E1/E2 push on SETUP = 'select' the current item."""
    item = SETUP_ITEMS[max(0, min(len(SETUP_ITEMS) - 1, focus.item))]
    if item.kind == "bool":
        return [Toggle(item.field)]
    if item.kind == "page":
        return [GotoPage(item.field)]
    if item.kind == "rebuild":
        if focus.confirm_now(now) == item.field:
            focus.confirm = None
            return [Toggle(item.field)]
        focus.confirm, focus.confirm_until = item.field, now + CONFIRM_WINDOW_S
        return [ShowHint(HINT_CONFIRM)]
    return []


def _hwtest(focus: Focus, event: Any, cmap: ControlMap, muted: Any = None) -> list[Action]:
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
    if kind == "long":
        if cmap.switch_role(idx) == ("B", 1):
            return [mute_action(muted)]
        return [GotoPage("SETUP")]
    return []


def knob_labels(page: str) -> Sequence[str]:
    """E1..E4 labels for `page` (pages.py draws them at KNOB_CORNERS)."""
    return [k.label for k in KNOBS.get(page, KNOBS["HWTEST"])]
