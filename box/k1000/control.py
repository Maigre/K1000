# SPDX-License-Identifier: GPL-3.0-or-later
"""K1000 controller: the one owner of the show state between the knobs, the screen, the engine
(CamillaDSP over its websocket) and the disk. docs/03-v0-architecture.md §2 and §4.

Why it is shaped like this:

- **One state, one apply worker.** UI actions only edit `self.state` (clamped by `State.set`) and
  kick the worker. The worker diffs what the engine holds (`_applied`) against the state and sends
  click-safe plans (`dspconfig.smooth_ops`). Fast knob turns therefore coalesce: while a plan
  runs, further detents pile up in the state and the next plan goes straight to the latest value.
  Setup actions (mono subs, audition, profile) come out of the diff as a `Rebuild`: they
  regenerate and upload the whole config with Main muted around it, and are refused unless the
  operator confirmed them (SETUP: chosen, then set by a push, D-013) or the box is muted.
- **Mute never waits.** B4 is the emergency gesture: `set_mute` sends the Main mute at once,
  outside the worker and its lock. Nothing else ever UNMUTES Main except the operator, a boot ramp,
  or a setup guard, and the last two re-check the operator's mute right before unmuting.
- **No ramp into an engine that is not processing.** CamillaDSP applies a fader change made while
  it is Stalled/Paused/Starting at once when audio resumes (README, Volume control), so a ramp
  run "into" a stalled capture is no ramp at all. The boot ramp and the setup guard unmute only
  once GetState reads Running (twice, > 2 chunks apart), check it again before every ramp step,
  and if the engine stops mid-ramp they mute + park Main again and restart the ramp later.
- **The engine is the truth while it plays.** A k1000 restart, a websocket blip or a CamillaGUI
  edit must never change the sound. On (re)connect, and about once a second, the controller reads
  the running config and faders and adopts what it owns (`dspconfig.state_from_config`), except
  local edits not sent yet (they win, then are sent). Values it owns but cannot adopt (fixed
  parameters, out-of-range values) are pushed back, but only in the SAFE direction and never as
  a step: a cut or a mute made outside K1000 (a Gain filter muted or turned down, the Aux1/Aux2
  group faders muted or lowered from CamillaGUI's side panel, e.g. measuring tops alone at the
  tune) is KEPT and shown (`status.drift`), never raised or unmuted by K1000 on its own; values
  above K1000's are pulled down in click-safe steps. An Aux group lowered outside K1000 keeps
  that offset under later balance moves.
- **A restarted ENGINE is detected, not guessed.** camilladsp starts with Main muted at -40 dB
  and every Aux fader at 0 dB (measured). Once K1000 has brought an engine in, it parks the unused
  Aux4 fader at `MARKER_DB` (no Volume filter listens to Aux4: no audible effect). So Main muted
  AT -40 and no marker = a fresh engine: push the saved state (safe while muted, devices included)
  and ramp Main in (`boot_ramp_plan`). Muted elsewhere without a marker = someone muted it: kept
  muted. Anything else = an engine that was already playing: adopt, never ramp, never drop the
  level.
- **Guards are recorded on the engine.** The exact marker value also says which faders K1000 is
  holding in a transient state (`marker_db`/`decode_marker`): an Aux group muted by a
  delay/polarity guard, Main muted by a setup guard or under the boot ramp. A k1000 killed inside
  that window is followed by one that knows the mute is K1000's (released / ramped in again),
  while a mute without that record is someone's decision and is never released.
- **Persistence is debounced and atomic** (`state.atomic_write`): state.json + active.yml, once
  the edits pause for `persist_debounce_s` (at most `persist_max_delay_s` after the first unsaved
  edit), and only when the content changed (SD card wear). An edit toward silence (the
  operator's mute, master turned down) is saved at once (at most every URGENT_SAVE_GAP_S): a
  power cut right after it must not boot the PA back louder or unmuted. A failed write is
  retried with a backoff (`SAVE_RETRY_S`). A torn state.json is rebuilt from active.yml
  (`recover_state`), the file the engine boots from, instead of overwriting the tune with
  defaults; the faders it cannot know come back MUTED.
- **Audio never depends on this process.** `stop()` waits for the engine work in flight (a
  restore, a guard), persists, and leaves the engine as it is; it never mutes.
"""
from __future__ import annotations

import asyncio
import collections
import copy
import dataclasses
import json
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

import yaml

from . import dspconfig as dc
from .cdsp import CdspError, CdspNoConfig, CdspUnavailable
from .dspconfig import FADER_MAIN, FADER_SUBS, FADER_TOPS, Fader, FaderMute, Op, Rebuild, SetValue
from .profiles import (PROC_ASOUND, PROFILE_NAMES, Profile, ProfileChoice, card_id, card_present,
                       load_profile)
from .state import (BOOL_FIELDS, CHOICES, N_SYS_PEQ, RANGES, REBUILD_FIELDS, State, atomic_write,
                    load_state)
from .ui.input_map import (
    A_LAPTOP, ALL_PAGES, AUTO_HOME_PAGES, BELL_SLOTS, EDIT_HIGHLIGHT_S, F_LOWCUT_ON, F_SPECTRUM_ON,
    HINT_S, HOME_AFTER_S, HOME_PAGE, LIVE_SLOT_LABELS, LIVE_SLOT_SHORT, LIVE_SLOTS, MUTE_TOGGLE,
    PAGES, ControlMap, Focus, GotoPage, Mute, NextPage, NotchHelper, Nudge, PhoneQr, PhoneToggle,
    B1_TAP, SETUP_ITEMS, ResetLive, SetChoice, SetupAction, Toggle, UndoReset, apply_focus, knob_tag_style,
    map_event, page_name, parse_knob_tags,
)

log = logging.getLogger(__name__)

# The unused Aux4 fader carries K1000's marker once K1000 has brought the engine in (see the
# module docstring): MARKER_DB + MARKER_STEP_DB * (sum of GUARD_BITS of the faders K1000 holds in
# a guard/ramp right now). Plain MARKER_DB = brought in, nothing in flight.
MARKER_FADER = 4
MARKER_DB = -61.8
MARKER_STEP_DB = 0.2
MARKER_TOL_DB = 0.05
GUARD_BITS = {FADER_SUBS: 1, FADER_TOPS: 2, FADER_MAIN: 4}
AUX_FADERS = (FADER_SUBS, FADER_TOPS)
AUX_NAMES = {FADER_SUBS: "SUBS", FADER_TOPS: "TOPS"}
AUX_FLOOR_DB = -150.0        # the engine's own fader floor

# camilladsp.service runs `camilladsp -m -g-40`: a freshly started engine reads Main muted here.
ENGINE_START_DB = dc.BOOT_GAIN_DB
FRESH_TOL_DB = 0.05
# The engine applies fader changes made while it is not processing as a step on resume: ramps
# and guard unmutes wait for Running, read twice this far apart (> 2 chunks of 256 @ 48 kHz).
RUN_CHECK_S = 0.012
RUN_WAIT_S = 0.05            # GetState poll while holding Main muted for the engine to run
BOOT_STEP_MAX_DB = 6.0       # no boot-ramp step above this, whatever boot_ramp_s says
MAX_DETENTS = 1000.0         # a sane encoder move (input_map acceleration stays far below)

POLL_S = 1.0                 # config + faders adoption, engine state, load, clipped samples
METER_S = 0.1                # output peak meters
OWN_WRITE_S = 0.5            # our own writes are not "external edits" for this long
PERSIST_DEBOUNCE_S = 1.0
PERSIST_MAX_DELAY_S = 5.0
URGENT_SAVE_GAP_S = 0.25        # saves toward silence (mute, master down): not more often
# Delay/polarity edits are applied under a group-fader mute (smooth_ops guard). Waiting for the
# knob to pause first turns a fast E3 turn into one dip instead of one dip per detent.
GUARD_SETTLE_S = 0.15
GUARD_SETTLE_MAX_S = 0.6
RESTORE_MIN_INTERVAL_S = 30.0   # at most one automatic pipeline restore per interval
FIX_MAX_REPEATS = 3             # identical drift fixes re-sent at most this often in a row
CLIP_DBFS = -0.1                # a playback peak at/above this lights the clip box
CLIP_HOLD_S = 3.0
IN_CLIP_DBFS = -0.5             # a capture (ADC) peak at/above this lights IN CLIP
METER_FLOOR_DB = -100.0
# The screen's meter shows -60..0 dBFS at ~4 px/dB: level changes below that floor or under
# METER_STEP_DB are invisible and must not wake the render loop (input noise hovering around
# the floor re-rendered identical frames at 10 fps: ~15 % of a core).
METER_SHOWN_FLOOR_DB = -60.0
METER_STEP_DB = 0.25
UPLOAD_TIMEOUT_S = 10.0
SYNC_RETRY_S = 2.0
SAVE_RETRY_S = (2.0, 5.0, 10.0)   # backoff after a failed state/config write (SD card trouble)
# SIGTERM budget (k1000.service TimeoutStopSec=5): wait for the edit in flight, then for the
# engine work in flight (a restore, a guard), then cancel.
STOP_SETTLE_S = 1.0
STOP_LOCK_S = 1.5

# Boot-ramp safety bounds for settings read from k1000.yaml (a typo must not boot the PA loud;
# BOOT_STEP_MAX_DB bounds the step size on top of these).
BOOT_GAIN_RANGE = (-80.0, -20.0)
BOOT_RAMP_RANGE = (0.5, 30.0)
# Client-side fader ceiling: Main never exceeds 0 dB, Aux carries +-balance/2 (<= +6 dB).
FADER_CEILING_DB = max(RANGES["master_db"].max, RANGES["balance_db"].max / 2.0)

DEVICE_NAMES = {"dev": "HiFiBerry", "m4": "MOTU M4"}
ENGINE_DOWN_STATES = ("Inactive", "Stalled")

HINT_STILL_MUTED = "STILL MUTED · HOLD B4 TO UNMUTE"
HINT_CONFIRM_SETUP = "MUTE OR CONFIRM ON SETUP (MAY CLICK)"
HINT_PINNED = "PROFILE PINNED IN k1000.yaml"
HINT_LIVE_RESET = "LIVE EQ RESET · UNDO IN SETUP"
HINT_LIVE_FLAT = "LIVE EQ ALREADY FLAT"
HINT_LIVE_RESTORED = "LIVE EQ RESTORED"
HINT_NOTHING_TO_UNDO = "NOTHING TO UNDO"
HINT_UNDO_LOST = "RESET UNDO LOST"
HINT_MUTE_CHANGED = "MUTE CHANGED · LOOK, HOLD B4 AGAIN"
HINT_MASTER_SLOW = "MASTER: SLOW DOWN"
HINT_NO_FREE_BAND = "NO FREE BAND"
HINT_NOTCH = "{band} · {hz} · E3 TO CUT"              # D-009: "P3 · 2.44k · E3 TO CUT"
HINT_NOTCH_NO_PEAK = "{band} · {hz} · NO PEAK · E3 TO CUT"
HINT_SPECTRUM_DISABLED = "SPECTRUM OFF IN k1000.yaml"
HINT_AUDITION_DEV_ONLY = "AUDITION: DEV PROFILE ONLY"
SETUP_PICK_FIELDS: frozenset[str] = frozenset(it.field for it in SETUP_ITEMS if it.picks)
HINT_NO_CHANGE = "NO CHANGE: ALREADY SET"
HINT_LAPTOP_NA = "LAPTOP ACCESS: NOT AVAILABLE"
HINT_LAPTOP_OPENING = "LAPTOP ACCESS: OPENING…"
HINT_LAPTOP_OPEN = "LAPTOP OPEN 2 H · GUI :5005 · SSH"
HINT_LAPTOP_CLOSED = "LAPTOP ACCESS CLOSED"
LAPTOP_HINT_S = 4.0

# D-009 master-up guard: master rises at most this much within any MASTER_UP_WINDOW_S (knobs,
# phone and console nudges alike); the detents beyond are refused with HINT_MASTER_SLOW. Going
# down is never limited. k1000.yaml `controls.master_up_guard_db` (0 = off).
MASTER_UP_GUARD_DB = 6.0
MASTER_UP_WINDOW_S = 2.0
# Reset live EQ keeps a one-level undo this long (and until the live EQ is edited again).
UNDO_RESET_S = 300.0
# The notch helper (B3 on SHOW): a free bell at 1/6 octave, at the spectrum PEAK if there is
# one; a target this close to a bell already in use selects that bell instead.
NOTCH_OCT = 1.0 / 6.0
NOTCH_Q = 1.0 / (2.0 * math.sinh(math.log(2.0) / 2.0 * NOTCH_OCT))     # 8.65
NOTCH_NEAR_OCT = 1.0 / 6.0
NOTCH_HINT_S = 4.0
UI_TICK_S = 0.5              # the auto-home check
MSG_HELD_MUTED = "NO AUDIO FLOWING · HELD MUTED"
# The knobs-offline alert sends Thomas to the phone mute only while there is one to use (phone
# servers on, SETUP › Phone on, the AP up: PhoneServer.remote_mute_ready); otherwise, after a
# no-go or with the phone switched off, to what is left (the operator card: source down, bypass).
MSG_KNOBS_OFFLINE = "KNOBS OFFLINE · SOURCE DOWN / BYPASS"
MSG_KNOBS_OFFLINE_PHONE = "KNOBS OFFLINE · USE PHONE MUTE / BYPASS"
MSG_PROFILE_ROUTING = "k1000.yaml ROUTING FIXED UP (SEE LOG)"
# Changes made to the engine by another client (CamillaGUI, the only other writer on the box):
# short, so the status bar shows them whole at a readable size (UX-5; docs/03 §3 has the table).
MSG_PIPELINE_CHANGED = "PIPELINE CHANGED (GUI)"
MSG_DEVICES_CHANGED = "DEVICES CHANGED (GUI)"
OUTSIDE = "(GUI)"
NOTE_REBUILT = "STATE REBUILT · CHECK MASTER"
NOTE_MISSING = "NO SAVED STATE · CHECK MASTER"
# Messages at/above this priority are alerts: also shown inside the MUTED band and on SETUP.
ALERT_PRIO = 4

# System alerts for the status bar (D-009 "alerts vs hints"): each controller message key's
# level. critical = red (KNOBS OFFLINE: full width), warning = orange, info = neither (a state
# K1000 is in, e.g. ramping in). Hints (amber, brief) are not alerts: they live in Focus.
ALERT_CRITICAL, ALERT_WARNING, ALERT_INFO = "critical", "warning", "info"
MESSAGE_LEVELS = {
    "knobs": ALERT_CRITICAL, "save": ALERT_CRITICAL, "engine": ALERT_CRITICAL,
    "settings": ALERT_WARNING, "profile": ALERT_WARNING, "wait": ALERT_WARNING,
    "drift": ALERT_WARNING, "devices": ALERT_WARNING, "held": ALERT_WARNING,
    "setup": ALERT_INFO, "ramp": ALERT_INFO,
}
# Status-bar ranks of the conditions the controller does not hold as messages (the body shows
# NO DEVICE / ENGINE DOWN in full; the status bar keeps KNOBS OFFLINE above them).
RANK_NO_DEVICE = 7.6
RANK_ENGINE_DOWN = 7.5


class _LinkChanged(CdspUnavailable):
    """The websocket link changed under a running plan: the next connect sync takes over."""


def marker_db(guards: Iterable[int] = ()) -> float:
    """The Aux4 marker value recording which faders K1000 holds in a guard/ramp."""
    bits = sum(GUARD_BITS[g] for g in set(guards) if g in GUARD_BITS)
    return round(MARKER_DB + MARKER_STEP_DB * bits, 3)


def decode_marker(vol: Any) -> frozenset[int] | None:
    """Aux4 volume -> the faders K1000 left in a guard/ramp (empty = none), or None when the
    value is not K1000's marker at all (an engine K1000 has not brought in)."""
    if isinstance(vol, bool) or not isinstance(vol, (int, float)) or not math.isfinite(vol):
        return None
    k = round((vol - MARKER_DB) / MARKER_STEP_DB)
    if not 0 <= k <= sum(GUARD_BITS.values()):
        return None
    if abs(vol - (MARKER_DB + k * MARKER_STEP_DB)) > MARKER_TOL_DB:
        return None
    return frozenset(g for g, b in GUARD_BITS.items() if k & b)


# =========================================================================== small types
@dataclass(frozen=True)
class Paths:
    """Where the controller persists. Defaults = docs/03 §5."""

    state: Path = Path("/var/lib/k1000/state.json")
    active_config: Path = Path("/var/lib/k1000/active.yml")

    @classmethod
    def from_settings(cls, settings: Any, *, state: str | Path | None = None,
                      active_config: str | Path | None = None) -> "Paths":
        """k1000.yaml `paths.{state, active_config}`, overridden by explicit arguments."""
        sec = settings.get("paths") if isinstance(settings, dict) else None
        sec = sec if isinstance(sec, dict) else {}
        d = cls()

        def pick(explicit: Any, key: str, default: Path) -> Path:
            if explicit:
                return Path(explicit)
            v = sec.get(key)
            return Path(v) if isinstance(v, str) and v.strip() else default

        return cls(pick(state, "state", d.state), pick(active_config, "active_config", d.active_config))


@dataclass(frozen=True)
class EngineSettings:
    """k1000.yaml `engine:` (url, boot_gain_db, boot_ramp_s), clamped to safe bounds."""

    url: str = "ws://127.0.0.1:1234"
    boot_gain_db: float = dc.BOOT_GAIN_DB
    boot_ramp_s: float = dc.BOOT_RAMP_S

    @classmethod
    def from_settings(cls, settings: Any, *, url: str | None = None) -> "EngineSettings":
        sec = settings.get("engine") if isinstance(settings, dict) else None
        sec = sec if isinstance(sec, dict) else {}
        d = cls()
        u = url or sec.get("url")
        u = u.strip() if isinstance(u, str) and u.strip().startswith(("ws://", "wss://")) else d.url
        return cls(u, _bounded(sec.get("boot_gain_db"), BOOT_GAIN_RANGE, d.boot_gain_db),
                   _bounded(sec.get("boot_ramp_s"), BOOT_RAMP_RANGE, d.boot_ramp_s))


@dataclass
class Status:
    """What the screen shows (pages.ScreenStatus fields, plus diagnostics).

    engine: GetState value, None while the websocket is down, "Unknown" before the first try.
    device_present: the profile's ALSA card(s) registered in /proc/asound (None = cannot tell).
    levels: peak dBFS of the 4 LOGICAL outputs (Sub L, Sub R, Top L, Top R), since the last read.
    clip: per logical output, latched CLIP_HOLD_S. muted: the engine's Main mute (None = unknown).
    """

    profile: str = ""
    device: str = ""
    device_present: bool | None = None
    engine: str | None = "Unknown"
    load: float | None = None
    levels: list[float] = field(default_factory=lambda: [METER_FLOOR_DB] * 4)
    clip: list[bool] = field(default_factory=lambda: [False] * 4)
    muted: bool | None = None
    message: str = ""
    stop_reason: str = ""
    clipped_samples: int | None = None
    connected: bool = False
    ramping: bool = False
    version: str = ""
    drift: list[str] = field(default_factory=list)
    ready: bool = False              # connect sync done: edits reach the engine
    alert: str = ""                  # most urgent message (priority >= ALERT_PRIO)
    in_clip: bool = False            # an input_map capture channel clipped (latched)
    knobs_ok: bool | None = None     # None = no knobs expected (--no-hw) / not reported
    # ---- UI v1 (D-009), refreshed by Controller.snapshot(); see docs in Controller.snapshot
    page: str = HOME_PAGE            # SHOW | ALIGN | SETUP | PHONE | HWTEST
    band: int = 0                    # selected live band, LIVE_SLOTS index (0-3 P1-P4, 4 LS, 5 HS)
    band_label: str = LIVE_SLOT_LABELS[0]
    band_short: str = LIVE_SLOT_SHORT[0]
    align_locked: bool = True        # ALIGN E3/E4 (delay, Lo-Cut) locked
    align_unlock_s: float = 0.0      # seconds left of the unlock (0 = locked): the EDIT pill
    pills: list[str] = field(default_factory=list)       # persistent non-default states, in order
    alerts: list[tuple[str, str]] = field(default_factory=list)   # (level, text), urgent first
    sys_alert: str = ""              # alerts[0] text ("" = none): the status bar's alert
    sys_alert_level: str = ""        # critical | warning | info | ""
    undo_reset_s: float = 0.0        # SETUP > Undo reset: seconds left (0 = nothing to undo)
    spectrum_on: bool = True         # SETUP > Spectrum (and not disabled in k1000.yaml)
    spectrum_state: str = "off"      # off | waiting | live | unavailable | disabled
    spectrum: Any = None             # eqgraph.RtaFrame (never mutated once published) or None
    peak_hz: float | None = None     # the spectrum's PEAK (pointer + frequency), None = none
    master_up_guard_db: float = MASTER_UP_GUARD_DB   # 0 = off
    laptop_s: float = 0.0            # SETUP > Laptop access: seconds left (0 = closed)
    laptop_state: str = "off"        # off (no such surface) | closed | opening | open | closing | unavailable
    knob_tags: str = "dim"           # the E1..E4 tags: k1000.yaml ui.knob_tags (dim | off | bright)

    def copy(self) -> "Status":
        return dataclasses.replace(self, levels=list(self.levels), clip=list(self.clip),
                                   drift=list(self.drift), pills=list(self.pills),
                                   alerts=list(self.alerts))


# =========================================================================== helpers
def master_up_guard_db(settings: Any) -> float:
    """k1000.yaml `controls.master_up_guard_db`: dB master may rise within MASTER_UP_WINDOW_S
    (0 = off). Anything unreadable -> MASTER_UP_GUARD_DB; bounded to 0..24 dB."""
    sec = settings.get("controls") if isinstance(settings, dict) else None
    v = sec.get("master_up_guard_db", MASTER_UP_GUARD_DB) if isinstance(sec, dict) else MASTER_UP_GUARD_DB
    if isinstance(v, bool):
        return MASTER_UP_GUARD_DB
    return _bounded(v, (0.0, 24.0), MASTER_UP_GUARD_DB)


def _same_spectrum(a: Any, b: Any) -> bool:
    """Two spectrum frames that would draw the same picture (scale, peak and every bin)."""
    if a is b:
        return True
    if a is None or b is None or getattr(a, "level", None) is None \
            or getattr(b, "level", None) is None:
        return False           # nothing to compare: redraw
    try:
        import numpy as np     # only with a spectrum (the module stays importable without it)
        for name in ("lo_db", "hi_db", "peak_hz"):
            if getattr(a, name, None) != getattr(b, name, None):
                return False
        for name in ("freqs", "level", "hold"):
            x, y = getattr(a, name, None), getattr(b, name, None)
            if x is None and y is None:
                continue
            if x is None or y is None or not np.array_equal(x, y):
                return False
        return True
    except Exception:   # noqa: BLE001 - when in doubt, redraw
        return False


def spectrum_enabled(settings: Any) -> bool:
    """k1000.yaml `spectrum.enabled` (default true). False: no spectrum task, no second engine
    connection; SETUP > Spectrum then says so. Only a real `false` turns it off."""
    sec = settings.get("spectrum") if isinstance(settings, dict) else None
    return not (isinstance(sec, dict) and sec.get("enabled") is False)


def fmt_hz_short(hz: float) -> str:
    """2440 -> '2.44k', 12500 -> '12.5k', 125 -> '125' (the notch hint)."""
    if hz >= 10000:
        return f"{hz / 1000:.1f}k"
    if hz >= 1000:
        return f"{hz / 1000:.2f}k"
    return f"{hz:.0f}"


def _live_dict(s: State) -> dict[str, Any]:
    d = s.to_dict()
    return {k: d[k] for k in ("live_peq", "live_lowshelf", "live_highshelf")}


def _audible_live(s: State) -> tuple[tuple[float, ...], ...]:
    """What the live EQ does to the sound: each band's gain, and its frequency and Q only when
    its gain is not 0 dB (a 0 dB peaking or shelf biquad is exactly unity, wherever it sits).
    The notch helper's Q/frequency write on a free bell leaves this unchanged."""
    out = []
    for b in (*s.live_peq, s.live_lowshelf, s.live_highshelf):
        g = float(b.gain)
        out.append((g, float(b.freq), float(b.q)) if g != 0.0 else (0.0,))
    return tuple(out)


def _log_master(old: Any, new: Any, source: str) -> None:
    """Every change of the Main level, with what made it (knob / phone / console / adoption),
    at INFO: a phantom detent and a hand must be told apart in the journal afterwards (the
    unexplained Main -19 -> -20 of 2026-10-12 20:25 could not be)."""
    try:
        log.info("master %.1f -> %.1f dB (%s)", float(old), float(new), source)
    except (TypeError, ValueError):
        log.info("master %r -> %r (%s)", old, new, source)


def _bounded(v: Any, rng: tuple[float, float], default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(f):
        return default
    return min(rng[1], max(rng[0], f))


def _k1000_profile(title: Any) -> str | None:
    """'K1000 m4' -> 'm4' (a config K1000 generated), else None."""
    if isinstance(title, str) and title.startswith("K1000 ") and title[6:] in PROFILE_NAMES:
        return title[6:]
    return None


def _fader(faders: Any, i: int) -> tuple[float | None, bool | None]:
    try:
        f = faders[i]
        vol, mute = f.get("volume"), f.get("mute")
    except (TypeError, IndexError, KeyError, AttributeError):
        return None, None
    vol = float(vol) if isinstance(vol, (int, float)) and not isinstance(vol, bool) else None
    return vol, mute if isinstance(mute, bool) else None


def _flat(d: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(d, dict):
        out: dict[str, Any] = {}
        for k, v in d.items():
            out.update(_flat(v, f"{prefix}{k}."))
        return out
    if isinstance(d, list):
        out = {}
        for i, v in enumerate(d):
            out.update(_flat(v, f"{prefix}{i}."))
        return out
    return {prefix[:-1]: d}


def changed_fields(a: State, b: State) -> dict[str, tuple[Any, Any]]:
    """{field path: (old, new)} between two states (for logs)."""
    fa, fb = _flat(a.to_dict()), _flat(b.to_dict())
    return {k: (fa.get(k), fb.get(k)) for k in sorted(set(fa) | set(fb)) if fa.get(k) != fb.get(k)}


def logical_levels(profile: Profile, audition: str, peaks: Any) -> list[float]:
    """Physical playback peaks -> the 4 logical outputs (Sub L, Sub R, Top L, Top R).

    m4-style profiles: output_map[physical] = logical. The dev audition profile has 2 physical
    outputs that carry mixes: in `sum` each side shows on both its sub and top bars; `tops`/`subs`
    show on their own bars only. Unknown or silent -> METER_FLOOR_DB.
    """
    vals: list[float] = []
    for v in peaks if isinstance(peaks, (list, tuple)) else []:
        f = float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else METER_FLOOR_DB
        vals.append(max(METER_FLOOR_DB, f) if math.isfinite(f) else METER_FLOOR_DB)
    out = [METER_FLOOR_DB] * 4
    if profile.audition:
        mapping = {"sum": {0: 0, 2: 0, 1: 1, 3: 1}, "tops": {2: 0, 3: 1},
                   "subs": {0: 0, 1: 1}}.get(audition, {0: 0, 2: 0, 1: 1, 3: 1})
        for logical, phys in mapping.items():
            if phys < len(vals):
                out[logical] = vals[phys]
    else:
        for phys, logical in enumerate(profile.output_map):
            if phys < len(vals) and 0 <= logical < 4:
                out[logical] = max(out[logical], vals[phys])
    return out


def _clamp_fader(idx: int, db: float) -> float:
    """Contract ranges for the faders K1000 drives (the client clamps again to its ceiling)."""
    if idx == FADER_MAIN:
        return RANGES["master_db"].clamp(db, RANGES["master_db"].min)
    if idx in (FADER_SUBS, FADER_TOPS):
        half = RANGES["balance_db"].max / 2.0
        return min(half, max(-half, RANGES["balance_db"].clamp(db, 0.0)))
    return db


def _lower(new: Any, old: Any) -> bool:
    try:
        return float(new) < float(old)
    except (TypeError, ValueError):
        return False


def _same_card(a: str, b: str, proc_root: str | Path = PROC_ASOUND) -> bool | None:
    """Do ALSA names `a` and `b` point at the same card? 'hw:0' and 'hw:sndrpihifiberry' do on
    the box (/proc/asound/<id> is a link to cardN). None = cannot tell (non-hw names, a card
    that is not registered)."""
    ia, ib = card_id(a), card_id(b)
    if ia is None or ib is None:
        return None
    if ia == ib:
        return True
    pa, pb = Path(proc_root) / ia, Path(proc_root) / ib
    try:
        if not pb.exists():
            return None
        if not pa.exists():
            return False          # the live device's card is gone: not the profile's card
        return pa.resolve() == pb.resolve()
    except OSError:
        return None


def _no_mute(s: State) -> State:
    c = s.copy()
    c.muted = False
    return c


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _keep_cuts(cfg: dict[str, Any], live: Any) -> dict[str, Any]:
    """`cfg` (a whole config about to replace `live`) with the cuts and mutes made outside K1000
    carried over: a Gain filter muted or turned down, a Biquad gain or Volume limit below
    K1000's. A pipeline restore puts the STRUCTURE back; it must not unmute an output or raise
    a trim nobody at the box asked for (the same rule as the 1 Hz fixes)."""
    lf = live.get("filters") if isinstance(live, dict) else None
    if not isinstance(lf, dict):
        return cfg
    out = copy.deepcopy(cfg)
    for name, f in (out.get("filters") or {}).items():
        lv = lf.get(name)
        if not isinstance(lv, dict) or lv.get("type") != f.get("type"):
            continue
        lp, p = lv.get("parameters"), f.get("parameters")
        if not isinstance(lp, dict) or not isinstance(p, dict):
            continue
        if f["type"] == "Gain":
            if lp.get("mute") is True:
                p["mute"] = True
            g, w = _num(lp.get("gain")), _num(p.get("gain"))
            if (g is not None and w is not None and g < w
                    and str(lp.get("scale") or "dB") == str(p.get("scale") or "dB")):
                p["gain"] = g
        elif f["type"] == "Biquad" and lp.get("type") == p.get("type"):
            g, w = _num(lp.get("gain")), _num(p.get("gain"))
            if g is not None and w is not None and g < w:
                p["gain"] = g
        elif f["type"] == "Volume":
            g, w = _num(lp.get("limit")), _num(p.get("limit"))
            if g is not None and w is not None and g < w:
                p["limit"] = g
    return out


def recover_state(paths: Paths) -> tuple[State, str]:
    """The state to start from: state.json (`state.load_state`), or, when state.json exists but
    cannot be read while active.yml holds a K1000 config, the state rebuilt from active.yml.

    Why: camilladsp boots from active.yml, so after a torn state.json that file IS the tune
    (system PEQ, trims, delays, polarity). Starting from defaults would overwrite active.yml at
    once and patch the defaults over the engine: the tune gone from both. Faders live only in
    state.json, so master/balance come back as defaults, and the calibrated master may be well
    below the default: the state starts MUTED (a fresh engine parks Main at the master and stays
    muted until the operator checks it and holds B4; an engine already playing is adopted as it
    plays, mute included). A MISSING state.json means defaults (first boot, or moved away on
    purpose: README "Sound state"; atomic writes never leave it missing), also MUTED when
    active.yml shows the box was set up before (not a first boot).
    Returns (state, note); note is "" on a normal start.
    """
    st = load_state(paths.state)        # logs, and keeps a .bad copy of a corrupt file
    missing = False
    try:
        data = json.loads(paths.state.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return st, ""
        why = "not a JSON object"
    except FileNotFoundError:
        missing = True
        why = "missing"
    except Exception as e:  # noqa: BLE001 - OSError, UnicodeDecodeError, ValueError...
        why = f"unreadable ({type(e).__name__})"
    try:
        cfg = yaml.safe_load(paths.active_config.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - no usable active.yml: first boot, or nothing to save
        return st, ""
    name = _k1000_profile(cfg.get("title")) if isinstance(cfg, dict) else None
    if name is None:
        return st, ""
    if missing:
        st = st.copy()
        st.muted = True
        log.warning("state.json missing while %s holds a K1000 config: defaults, MUTED (check "
                    "the master before unmuting)", paths.active_config)
        return st, NOTE_MISSING
    base = State()
    base.profile = name                  # dev_audition is read from the mixers only for dev
    rebuilt = dc.state_from_config(cfg, None, base)
    rebuilt.muted = True
    log.warning("state.json %s: state rebuilt from %s (the tune the engine boots from); "
                "master/balance are defaults, MUTED until the operator checks the master", why,
                paths.active_config)
    return rebuilt, NOTE_REBUILT


# =========================================================================== controller
class Controller:
    """The brain: `Controller(client, profile, state, paths)` + `await start()`.

    UI actions are synchronous and never block (they edit the state and schedule the engine
    work): `nudge`, `set_value`, `toggle`, `reset_live`, `set_mute`, `next_page`, `goto_page`,
    `show_hint`, or `handle_event` / `handle_action` for input_map events and actions.
    `await settle()` waits until the engine has everything (tests, shutdown).
    """

    def __init__(
        self,
        client: Any,
        profile: Profile,
        state: State,
        paths: Paths,
        *,
        settings: dict[str, Any] | None = None,
        profile_choice: ProfileChoice | None = None,
        boot_gain_db: float = dc.BOOT_GAIN_DB,
        boot_ramp_s: float = dc.BOOT_RAMP_S,
        transform: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        profile_loader: Callable[[str], Profile] | None = None,
        device_probe: Callable[[str], bool | None] | None = card_present,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        poll_s: float = POLL_S,
        meter_s: float = METER_S,
        persist_debounce_s: float = PERSIST_DEBOUNCE_S,
        persist_max_delay_s: float = PERSIST_MAX_DELAY_S,
    ) -> None:
        self.client = client
        self.profile = profile
        self.paths = paths
        self.settings: dict[str, Any] = settings if isinstance(settings, dict) else {}
        self.profile_choice = profile_choice
        self.boot_gain_db = _bounded(boot_gain_db, BOOT_GAIN_RANGE, dc.BOOT_GAIN_DB)
        self.boot_ramp_s = _bounded(boot_ramp_s, BOOT_RAMP_RANGE, dc.BOOT_RAMP_S)
        self._transform = transform
        self._profile_loader = profile_loader or (lambda name: load_profile(name, self.settings))
        self._device_probe = device_probe
        self._sleep = sleep
        self._clock = clock
        self.poll_s = poll_s
        self.meter_s = meter_s
        self.persist_debounce_s = persist_debounce_s
        self.persist_max_delay_s = persist_max_delay_s

        s = state.clamped()
        s.profile = profile.name          # the state always names the running profile
        self.state: State = s
        self._applied: State = s.copy()   # what the engine is believed to hold
        self.page = HOME_PAGE
        self.focus = Focus()
        self.controls = ControlMap.from_cfg(self.settings)
        # UI timers (auto-home, the master-up window, the reset undo): time.monotonic, like
        # the knob events' timestamps. Replaceable in tests.
        self._ui_clock: Callable[[], float] = time.monotonic
        self._last_input = self._ui_clock()
        self.master_up_guard_db = master_up_guard_db(self.settings)
        self._master_ups: collections.deque[tuple[float, float]] = collections.deque()
        self._undo: dict[str, Any] | None = None       # reset_live's one-level undo
        # The SHOW spectrum (rta.SpectrumTask, set by __main__; None = none) and whether
        # k1000.yaml allows it at all (`spectrum: enabled: false` = no task, no second link).
        self.rta: Any = None
        self.spectrum_enabled = spectrum_enabled(self.settings)
        # Extra persistent pills (e.g. the laptop access countdown): callables returning a
        # short text or None, read at every snapshot. Never allowed to break the screen.
        self.pill_sources: list[Callable[[], str | None]] = []
        # SETUP action items beyond reset/undo (input_map.SetupAction): name -> callable, run
        # after the item's confirm. Unknown names only say so.
        self.setup_actions: dict[str, Callable[[], Any]] = {}
        # SETUP > Laptop access (laptop.LaptopAccess, set by __main__ through attach_laptop;
        # None = no such surface: the item then says NOT AVAILABLE).
        self.laptop: Any = None
        self._laptop_seen: str | None = None
        self.status = Status(profile=profile.name, device=DEVICE_NAMES.get(profile.name, profile.name))
        self.status.knob_tags = knob_tag_style(self.settings)
        ui_sec = self.settings.get("ui")
        raw_tags = ui_sec.get("knob_tags") if isinstance(ui_sec, dict) else None
        if raw_tags is not None and parse_knob_tags(raw_tags) is None:
            log.warning("k1000.yaml ui.knob_tags %r is not dim | off | bright: using %s",
                        raw_tags, self.status.knob_tags)
        self.changed = asyncio.Event()
        self.version = 0
        self.phone: Any = None            # web.PhoneServer, set by __main__ (D-007); None = no phone
        # Mute generation (mute_view): moves on every mute request and every change of the mute
        # the operator sees, so an unmute decided on an older view is refused (set_mute(seen=)).
        self.mute_seq = 0
        self._mute_seen: tuple[bool, bool] | None = None

        self._engine_ready = False        # connect sync done on the current link
        self._syncing = False
        self._ramping = False
        self._busy = False                # the worker is running a plan
        self._epoch = 0                   # bumped on every connect/disconnect
        self._gen = 0                     # bumped on every local edit
        self._engine_lock = asyncio.Lock()
        self._kick = asyncio.Event()
        self._mute_want: bool | None = None
        self._mute_task: asyncio.Task[None] | None = None
        self._own_ptr: dict[str, float] = {}
        self._own_fader: dict[int, float] = {}
        self._dirty_since: float | None = None
        self._last_change = 0.0
        self._persist_kick = asyncio.Event()
        self._saved_state_text: str | None = None
        self._saved_cfg_text: str | None = None
        self._yaml_cache: tuple[str, str] | None = None   # (the config as JSON, its YAML)
        self._write_lock = threading.Lock()
        self._clip_until = [0.0] * 4
        self._peak_hold = [METER_FLOOR_DB] * 4
        self._last_clipped: int | None = None
        self._rebuild_seen = 0
        self._last_restore = -math.inf
        self._fix_key: Any = None
        self._fix_repeats = 0
        self._save_failures = 0
        # Faders K1000 may have left in a guard/ramp on the engine (mirrored in the marker):
        # set before the guard's mute, removed once its unmute is confirmed.
        self._guards: set[int] = set()
        self._marker_engine: float | None = None       # Aux4 as last read or written
        # Aux groups lowered (offset <= 0 dB under the balance macro) or muted outside K1000:
        # kept, shown, never raised or released by K1000.
        self._aux_offset: dict[int, float] = {i: 0.0 for i in AUX_FADERS}
        self._aux_ext_mute: set[int] = set()
        self._messages: dict[str, tuple[int, str]] = {}
        self._urgent_save = False         # an edit toward silence: save without the debounce
        self._last_save = -math.inf
        self._in_clip_until = 0.0
        self._meter_key: Any = None
        self._device_seen = 0             # polls in a row with the engine on the wrong card
        self._last_repair = -math.inf
        # Is `a` the same ALSA card as `b`? (/proc/asound/<id> -> cardN). Injectable for tests.
        self.same_card: Callable[[str, str], bool | None] = _same_card
        self._logged: set[Any] = set()
        self._tasks: list[asyncio.Task[Any]] = []
        self._started = False
        self._stop_called = False
        self._stopping = False
        if profile_choice is not None and profile_choice.error:
            self._set_message("settings", 5, f"SETTINGS: {profile_choice.error}")
        for w in profile.warnings:
            log.warning("profile %s: %s", profile.name, w)
        self._note_profile_routing(profile)

    # ------------------------------------------------------------------ lifecycle
    @property
    def profile_pinned(self) -> bool:
        """k1000.yaml names the profile: SETUP may not switch it (gig rule: m4 pinned)."""
        return self.profile_choice is not None and self.profile_choice.source == "settings"

    @property
    def engine_ready(self) -> bool:
        return self._engine_ready

    @property
    def ramping(self) -> bool:
        return self._ramping

    async def start(self, connect_timeout: float | None = 0.0) -> None:
        """Write active.yml (the engine unit waits for a valid one), hook into the client,
        start the link and the background tasks. Returns at once; the connect sync runs in the
        client's on_connect hook."""
        if self._started:
            return
        self._started = True
        self.save_now()
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        loop = asyncio.get_running_loop()
        for coro, name in ((self._worker(), "k1000-apply"), (self._poll_loop(), "k1000-poll"),
                           (self._meter_loop(), "k1000-meter"), (self._persist_loop(), "k1000-persist"),
                           (self._ui_loop(), "k1000-ui")):
            self._tasks.append(loop.create_task(coro, name=name))
        self._refresh_device()
        already = bool(getattr(self.client, "connected", False))
        ok = await self.client.connect(timeout=connect_timeout)
        if already and ok:   # connected before our hook was set: sync ourselves
            self._tasks.append(loop.create_task(self._on_connect(self.client), name="k1000-sync"))

    async def stop(self, settle_timeout: float = STOP_SETTLE_S) -> None:
        """Persist and stop the controller's tasks. Never mutes, never touches the engine beyond
        finishing the work in flight: audio must survive a UI restart.

        Order: let the edit in flight reach the engine (settle), stop starting new engine work,
        wait for the work still holding the engine (a poll-driven restore between its Main mute
        and unmute, a guard), then cancel. Work that still does not finish (an engine that does
        not process) is cancelled; a guard it leaves is recorded on the marker and finished by
        the next k1000."""
        if self._stop_called:
            return
        self._stop_called = True
        try:   # let the edit in flight reach the engine: screen, engine and disk then agree
            await asyncio.wait_for(self.settle(), settle_timeout)
        except (TimeoutError, asyncio.TimeoutError):
            log.warning("stopping with engine work still pending")
        self._stopping = True
        locked = False
        try:
            await asyncio.wait_for(self._engine_lock.acquire(), STOP_LOCK_S)
            locked = True
        except (TimeoutError, asyncio.TimeoutError):
            log.warning("engine work still running after %.1f s: cancelling it (a guard it leaves "
                        "is recorded on the engine and finished by the next k1000)", STOP_LOCK_S)
        try:
            if self._mute_task is not None and not self._mute_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(self._mute_task), 0.5)
                except BaseException:
                    pass
            if getattr(self.client, "on_connect", None) == self._on_connect:
                self.client.on_connect = None
            if getattr(self.client, "on_disconnect", None) == self._on_disconnect:
                self.client.on_disconnect = None
            for t in self._tasks:
                t.cancel()
            for t in self._tasks:
                try:
                    await t
                except BaseException:
                    pass
            self._tasks.clear()
        finally:
            if locked:
                self._engine_lock.release()
        self.save_now()

    async def settle(self, timeout: float | None = None) -> bool:
        """Wait until the engine holds the state (or the engine is down: nothing to wait for).
        Engine work started by the poll (an automatic restore, drift fixes) counts too: it holds
        the engine lock."""
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while True:
            busy = (self._syncing or self._busy or self._ramping or self._engine_lock.locked()
                    or (self._mute_want is not None and self.client.connected)
                    or (self._engine_ready and self.client.connected and bool(self._pending_ops())))
            if not busy:
                return True
            if deadline is not None and loop.time() > deadline:
                return False
            await asyncio.sleep(0.005)

    async def wait_changed(self, timeout: float | None = None) -> bool:
        """For the render loop: True when something visible changed (else False on timeout)."""
        try:
            await asyncio.wait_for(self.changed.wait(), timeout)
        except (TimeoutError, asyncio.TimeoutError):
            return False
        self.changed.clear()
        return True

    def snapshot(self) -> tuple[str, State, Status, Focus]:
        """Copies safe to hand to a render thread: (page, state, status, focus).

        The UI v1 fields of the status are refreshed here (the render contract):
        - page; band / band_label / band_short: the selected live band (Focus.slot);
        - align_locked / align_unlock_s: ALIGN's E3/E4 lock and the EDIT countdown;
        - pills: persistent non-default states: `pill_sources` first (the laptop-access
          countdown: the only sign that SSH and CamillaGUI are open on the K1000 Wi-Fi), then
          LO-CUT OFF, POL INV SUB|TOP|SUB+TOP, MONO SUB, AUDITION TOPS|SUBS;
        - alerts / sys_alert / sys_alert_level: system alerts for the status bar, most urgent
          first (critical red, warning orange, info); hints stay in Focus (amber, brief);
        - undo_reset_s: SETUP > Undo reset availability;
        - spectrum_on / spectrum_state / spectrum / peak_hz: the SHOW spectrum (set by the
          spectrum task through set_spectrum; None when off, stale or not on SHOW);
        - laptop_s / laptop_state: SETUP > Laptop access (seconds left, what root reports).
        """
        now = self._ui_clock()
        st = self.status
        st.ready = self._engine_ready
        st.page = self.page
        st.band = self.focus.band
        st.band_label = LIVE_SLOT_LABELS[st.band]
        st.band_short = LIVE_SLOT_SHORT[st.band]
        st.align_unlock_s = round(self.focus.unlock_left(now), 3)
        st.align_locked = st.align_unlock_s <= 0.0
        st.pills = self.pills()
        st.alerts = self.alerts()
        st.sys_alert, st.sys_alert_level = (st.alerts[0][1], st.alerts[0][0]) if st.alerts else ("", "")
        st.undo_reset_s = round(self.undo_left(now), 3)
        st.spectrum_on = self.spectrum_enabled and bool(self.state.spectrum_on)
        if not self.spectrum_enabled or self.rta is None:
            st.spectrum_state = "disabled" if not self.spectrum_enabled else "off"
            st.spectrum, st.peak_hz = None, None
        st.master_up_guard_db = self.master_up_guard_db
        st.laptop_s, st.laptop_state = self._laptop_view()
        self.focus.readonly = self.setup_readonly()
        return self.page, self.state.copy(), st.copy(), copy.deepcopy(self.focus)

    # ------------------------------------------------------------------ UI v1 views
    def pills(self) -> list[str]:
        """Persistent pills for any non-default system state (D-009). Spectrum OFF has none.
        The extra sources (LAPTOP OPEN) come FIRST: the screen folds the last ones into '+N'
        when they do not fit, and an open laptop access must never be the one hidden."""
        s = self.state
        out: list[str] = []
        for src in list(self.pill_sources):
            try:
                t = src()
            except Exception as e:   # noqa: BLE001 - a pill must never break the screen
                self._log_once(("pill", repr(src), type(e).__name__), logging.ERROR,
                               "pill source %r failed: %s", src, e)
                continue
            if t:
                out.append(str(t))
        if not s.lowcut_on:
            out.append("LO-CUT OFF")
        inv = [n for n, on in (("SUB", s.sub_inverted), ("TOP", s.top_inverted)) if on]
        if inv:
            out.append("POL INV " + "+".join(inv))
        if s.mono_subs:
            out.append("MONO SUB")
        if self.profile.audition and s.dev_audition != "sum":
            out.append(f"AUDITION {str(s.dev_audition).upper()}")
        return out

    def alerts(self) -> list[tuple[str, str]]:
        """(level, text) of every system alert, most urgent first: the controller's messages
        (MESSAGE_LEVELS) plus NO DEVICE and ENGINE DOWN."""
        ranked: list[tuple[float, str, str]] = []
        for key, (prio, text) in self._messages.items():
            level = MESSAGE_LEVELS.get(key, ALERT_CRITICAL if prio >= ALERT_PRIO else ALERT_WARNING)
            ranked.append((float(prio), level, text))
        if self.status.device_present is False:
            ranked.append((RANK_NO_DEVICE, ALERT_CRITICAL, f"NO DEVICE · {self.status.device}"))
        eng = self.status.engine
        if eng is None or eng in ENGINE_DOWN_STATES:
            ranked.append((RANK_ENGINE_DOWN, ALERT_CRITICAL,
                           "ENGINE DOWN" if eng is None else f"ENGINE {str(eng).upper()}"))
        ranked.sort(key=lambda r: -r[0])
        return [(level, text) for _r, level, text in ranked]

    def set_knobs_ok(self, ok: bool | None) -> None:
        """The knob service reports whether the MCP23017 answers (None = no knobs expected).
        While it does not, the screen says so: B4 (mute) does nothing then."""
        if ok == self.status.knobs_ok:
            return
        if ok is False:
            log.error("knobs offline: B4 mute and the knobs do nothing until they recover")
        elif ok and self.status.knobs_ok is False:
            log.warning("knobs back online")
        self.status.knobs_ok = ok
        self._set_message("knobs", 8, self._knobs_offline_text() if ok is False else None)
        self._changed()

    def _knobs_offline_text(self) -> str:
        ph = self.phone
        try:
            ready = ph is not None and ph.remote_mute_ready is True
        except Exception:  # noqa: BLE001 - a phone surface bug must not hide the alert
            ready = False
        return MSG_KNOBS_OFFLINE_PHONE if ready else MSG_KNOBS_OFFLINE

    def phone_changed(self) -> None:
        """The phone surface changed (SETUP › Phone, the AP, its servers): while the knobs are
        offline, the alert says whether a phone mute is there to use."""
        if self.status.knobs_ok is False:
            self._set_message("knobs", 8, self._knobs_offline_text())

    # ------------------------------------------------------------------ UI actions
    def nudge(self, field: str, detents: int | float, fine: bool = False,
              source: str = "knob") -> Any:
        """Move a numeric field by encoder detents (input_map already applied acceleration).
        Non-finite or absurd detent counts are refused: Range.clamp would turn inf/nan into the
        field's DEFAULT (master -80 -> -20 dB), and huge ones overflow log fields. `source`
        (knob / phone / console) is logged with every master change."""
        try:
            d = float(detents)
        except (TypeError, ValueError, OverflowError):
            d = math.nan
        if not math.isfinite(d) or abs(d) > MAX_DETENTS:
            log.warning("nudge %r by %r detents refused (not an encoder move)", field, detents)
            return None
        n: int | float = detents if isinstance(detents, int) else d
        if field == "master_db" and d > 0:
            n = self._master_up_allowed(n, fine)
            if not n:
                return None
        try:
            old = self.state.get(field)
            # ALIGN's signed delay writes BOTH stored delays (D-011): a move that normalises a
            # sub+top pair is an edit even where the net value would read the same.
            pair = (self.state.sub_delay_ms, self.state.top_delay_ms)
            new = self.state.nudge(field, n, fine)
        except (KeyError, ValueError, TypeError, ArithmeticError) as e:
            log.warning("nudge %r refused: %s", field, e)
            return None
        if new != old or pair != (self.state.sub_delay_ms, self.state.top_delay_ms):
            if field == "master_db":
                if new > old:
                    self._master_ups.append((self._ui_clock(), float(new) - float(old)))
                _log_master(old, new, source)
            self._local_edit(urgent=field == "master_db" and _lower(new, old))
        return new

    def _master_up_allowed(self, detents: int | float, fine: bool) -> int | float:
        """D-009 master-up guard: how many of `detents` (> 0) master may rise now, so that it
        rises at most master_up_guard_db within any MASTER_UP_WINDOW_S. Refused detents say
        so (HINT_MASTER_SLOW). Counts what was really raised (a clamp at 0 dB costs nothing)."""
        cap = self.master_up_guard_db
        if cap <= 0:
            return detents
        now = self._ui_clock()
        win = self._master_ups
        while win and now - win[0][0] >= MASTER_UP_WINDOW_S:
            win.popleft()
        room = cap - sum(db for _t, db in win)
        rng = RANGES["master_db"]
        cur = float(self.state.master_db)
        if rng.nudge(cur, detents, fine) - cur <= room + 1e-9:
            return detents
        step = rng.fine if (fine and rng.fine) else rng.step
        allowed = max(0, math.floor(room / step + 1e-9)) if room > 0 else 0
        log.warning("master-up guard: %s of %s detent(s) up refused (%.1f dB already raised in "
                    "the last %g s)", detents - allowed, detents, cap - room, MASTER_UP_WINDOW_S)
        self.show_hint(HINT_MASTER_SLOW)
        return int(allowed) if isinstance(detents, int) else float(allowed)

    def set_value(self, field: str, value: Any, confirmed: bool = False,
                  source: str = "local") -> Any:
        """Set one field directly (clamped). Rebuild fields follow the `toggle` rules. `source`
        (phone / console / ...) is logged with every master change."""
        if field == "muted":
            return self.set_mute(bool(value))
        if field == F_LOWCUT_ON and isinstance(value, bool):
            return self.set_lowcut(value)     # the phone's switch: the box's path (hint, log)
        if field in REBUILD_FIELDS and not self._setup_allowed(field, confirmed):
            return None
        try:
            old = self.state.get(field)
            pair = (self.state.sub_delay_ms, self.state.top_delay_ms)   # see nudge
            new = self.state.set(field, value)
        except (KeyError, ValueError, TypeError) as e:
            log.warning("set %r=%r refused: %s", field, value, e)
            return None
        if new != old or pair != (self.state.sub_delay_ms, self.state.top_delay_ms):
            if field == "master_db":
                _log_master(old, new, source)
            self._local_edit(urgent=field == "master_db" and _lower(new, old))
        return new

    def toggle(self, field: str, confirmed: bool = False) -> bool:
        """Flip a bool field, or step an enum field (`dev_audition`, `profile`) to its next choice.

        Pipeline-rebuilding fields (state.REBUILD_FIELDS) are refused unless `confirmed` (the
        SETUP choice set by a push) or the box is muted; `profile` is refused while k1000.yaml
        pins it.
        """
        if field == "muted":
            self.set_mute(MUTE_TOGGLE)
            return True
        if field in REBUILD_FIELDS and not self._setup_allowed(field, confirmed):
            return False
        if field in BOOL_FIELDS:
            self.state.set(field, not bool(self.state.get(field)))
        elif field in CHOICES:
            choices = CHOICES[field]
            cur = self.state.get(field)
            nxt = choices[(choices.index(cur) + 1) % len(choices)] if cur in choices else choices[0]
            self.state.set(field, nxt)
        else:
            log.warning("toggle %r: not a bool/choice field", field)
            return False
        log.info("toggle %s -> %r", field, self.state.get(field))
        self._local_edit()
        return True

    def setup_readonly(self) -> dict[str, str]:
        """SETUP fields that cannot change now -> the hint the knobs show (Focus.readonly):
        Profile while k1000.yaml pins it, Audition off the dev profile, Spectrum while k1000.yaml
        disables it. The screen draws their values dimmed."""
        out: dict[str, str] = {}
        if self.profile_pinned:
            out["profile"] = HINT_PINNED
        if not getattr(self.profile, "audition", False):
            out["dev_audition"] = HINT_AUDITION_DEV_ONLY
        if not self.spectrum_enabled:
            out[F_SPECTRUM_ON] = HINT_SPECTRUM_DISABLED
        return out

    def set_choice(self, field: str, value: Any) -> bool:
        """SETUP (D-013): set `field` to the value the row showed, chosen with E2 and set by a
        push (two gestures: the confirm a pipeline rebuild needs). Refused, and said, when the
        field cannot change now (Focus.readonly) or the value is not one of its choices; a
        value already set (a phone changed it meanwhile) changes nothing and says so."""
        if field not in SETUP_PICK_FIELDS:      # never `muted`: that is set_mute's, with its guards
            log.warning("set_choice %r: not a SETUP choice row", field)
            return False
        ro = self.setup_readonly().get(field)
        if ro:
            self.show_hint(ro)
            return False
        if field == F_LOWCUT_ON:
            if bool(value) == bool(self.state.lowcut_on):
                self.show_hint(HINT_NO_CHANGE)
                return False
            self.set_lowcut(bool(value))
            return True
        if field in BOOL_FIELDS:
            value = bool(value)
        elif field not in CHOICES or value not in CHOICES[field]:
            log.warning("set_choice %r = %r: not a SETUP choice", field, value)
            return False
        if self.state.get(field) == value:
            self.show_hint(HINT_NO_CHANGE)
            return False
        if field in REBUILD_FIELDS and not self._setup_allowed(field, confirmed=True):
            return False
        self.state.set(field, value)
        log.info("setup %s -> %r", field, value)
        self._local_edit()
        if field == F_SPECTRUM_ON:
            self.show_hint("SPECTRUM ON" if value else "SPECTRUM OFF")
        return True

    def reset_live(self) -> None:
        """SETUP > Reset live EQ (confirmed), or the phone's confirmed reset: the live EQ layer
        back to flat (smoothed by the worker). Keeps a one-level undo (`undo_reset`) for
        UNDO_RESET_S, or until the live EQ is edited audibly again, whoever edits it (see
        undo_left)."""
        new = self.state.live_reset()
        if new.to_dict() == self.state.to_dict():
            self.show_hint(HINT_LIVE_FLAT)
            return
        before = _live_dict(self.state)
        self.state = new
        self._undo = {"before": before, "after": _audible_live(new),
                      "until": self._ui_clock() + UNDO_RESET_S}
        log.info("live EQ reset (undo kept %g s): was %s", UNDO_RESET_S, before)
        self.show_hint(HINT_LIVE_RESET)
        self._local_edit()

    def undo_left(self, now: float | None = None) -> float:
        """Seconds the reset undo stays available (0.0 = none). It ends at the first AUDIBLE
        live EQ edit after the reset (a gain moved off 0 dB, by a knob, the phone or CamillaGUI
        adopted; freq/Q of a band at 0 dB is inaudible and keeps it: the notch helper's B3, an
        E2/E4 turn on a flat band), or after UNDO_RESET_S. Undo then restores the whole live
        EQ from before the reset, over any such inaudible move. Dropped by an edit, it says
        so (RESET UNDO LOST)."""
        u = self._undo
        if u is None:
            return 0.0
        now = self._ui_clock() if now is None else now
        if now >= u["until"]:
            self._undo = None
            return 0.0
        if _audible_live(self.state) != u["after"]:
            self._undo = None
            log.info("live EQ reset undo dropped: the live EQ was edited (audibly) since")
            self.show_hint(HINT_UNDO_LOST)
            return 0.0
        return u["until"] - now

    def undo_reset(self) -> bool:
        """SETUP > Undo reset: the live EQ exactly as it was before the last reset (the worker
        smooths the way back). One level: the undo is gone once used."""
        if self.undo_left() <= 0.0:
            self.show_hint(HINT_NOTHING_TO_UNDO)
            return False
        u, self._undo = self._undo, None
        assert u is not None
        new = State.from_dict({**self.state.to_dict(), **u["before"]})
        new.profile = self.state.profile
        self.state = new
        log.info("live EQ reset undone: %s", u["before"])
        self.show_hint(HINT_LIVE_RESTORED)
        self._local_edit()
        return True

    def notch_helper(self, now: float | None = None) -> int | None:
        """B3 on SHOW (D-009). Returns the slot it selected (None: no free band).

        1. A spectrum PEAK within NOTCH_NEAR_OCT of a bell already in use (gain != 0): select
           that bell, write nothing.
        2. Else a free bell (gain exactly 0; the selected band if it is one, else the first of
           P1..P4, never a shelf): Q = NOTCH_Q (1/6 octave), freq = the PEAK if there is one.
           The gain is never touched: a 0 dB peaking biquad is exactly unity, so this is
           inaudible until E3 cuts.
        3. Else: hint NO FREE BAND, nothing changes.
        The selected band moves there, its GAIN gets the edit highlight, the hint says
        "P3 · 2.44k · E3 TO CUT"."""
        target = self._peak_target()
        peq = self.state.live_peq
        used = [i for i in BELL_SLOTS if i < len(peq) and float(peq[i].gain) != 0.0]
        if target is not None:
            near = [(abs(math.log2(float(peq[i].freq) / target)), i) for i in used
                    if float(peq[i].freq) > 0]
            near = [x for x in near if x[0] <= NOTCH_NEAR_OCT + 1e-9]
            if near:
                i = min(near)[1]
                log.info("notch helper: peak %.0f Hz is band P%d's (%.0f Hz): selected", target,
                         i + 1, peq[i].freq)
                self._notch_select(i, HINT_NOTCH, now)
                return i
        sel = self.focus.band
        free = [i for i in BELL_SLOTS if i < len(peq) and float(peq[i].gain) == 0.0]
        if not free:
            self.show_hint(HINT_NO_FREE_BAND, NOTCH_HINT_S)
            return None
        i = sel if sel in free else free[0]
        base = LIVE_SLOTS[i]
        old = (self.state.get(f"{base}.freq"), self.state.get(f"{base}.q"))
        self.state.set(f"{base}.q", NOTCH_Q)
        if target is not None:
            self.state.set(f"{base}.freq", target)
        if (self.state.get(f"{base}.freq"), self.state.get(f"{base}.q")) != old:
            self._local_edit()
        log.info("notch helper: P%d at %.0f Hz, Q %.2f%s (gain untouched)", i + 1,
                 self.state.get(f"{base}.freq"), self.state.get(f"{base}.q"),
                 "" if target is not None else ", no peak")
        self._notch_select(i, HINT_NOTCH if target is not None else HINT_NOTCH_NO_PEAK, now)
        return i

    def _peak_target(self) -> float | None:
        if not (self.spectrum_enabled and self.state.spectrum_on):
            return None
        hz = self.status.peak_hz
        if isinstance(hz, (int, float)) and not isinstance(hz, bool) and math.isfinite(hz) and hz > 0:
            return float(hz)
        return None

    def _notch_select(self, slot: int, template: str, now: float | None) -> None:
        now = time.monotonic() if now is None else now
        self.focus.slot = slot
        self.focus.editing = f"{LIVE_SLOTS[slot]}.gain"
        self.focus.edit_until = now + max(EDIT_HIGHLIGHT_S, NOTCH_HINT_S)
        hz = float(self.state.get(f"{LIVE_SLOTS[slot]}.freq"))
        self.show_hint(template.format(band=LIVE_SLOT_SHORT[slot], hz=fmt_hz_short(hz)), NOTCH_HINT_S)

    def set_lowcut(self, on: bool) -> bool:
        """Lo-Cut on/off: SETUP > Lo-Cut (after its confirm) and the phone's switch (confirmed
        on the phone). Off = parked at LOWCUT_OFF_HZ, a frequency move (no pipeline change).
        Said on the screen either way. Returns the new state."""
        on = bool(on)
        if bool(self.state.lowcut_on) != on:
            self.state.set(F_LOWCUT_ON, on)
            log.info("Lo-Cut %s", "ON" if on else "OFF")
            self._local_edit()
        self.focus.editing, self.focus.edit_until = F_LOWCUT_ON, time.monotonic() + EDIT_HIGHLIGHT_S
        self.show_hint(f"LO-CUT ON · {self.state.lowcut_hz:.0f} Hz" if on else "LO-CUT OFF")
        return on

    # ------------------------------------------------------------------ spectrum (SHOW)
    def spectrum_wanted(self) -> bool:
        """The spectrum task polls only while this holds: SHOW displayed, SETUP > Spectrum ON,
        the engine reachable and processing (Running, or Paused on silence), device present."""
        st = self.status
        return (self.spectrum_enabled and self.page == "SHOW" and bool(self.state.spectrum_on)
                and bool(st.connected) and st.engine in ("Running", "Paused")
                and st.device_present is not False)

    def set_spectrum(self, frame: Any, state: str) -> None:
        """From the spectrum task: the frame to draw (None = no picture) and its state. Wakes
        the screen only: `version` is not bumped (phones do not show the spectrum). A frame
        identical to the one shown (silence: every bin at the floor, the scale settled) wakes
        nothing, so a quiet SHOW page keeps the idle render rate."""
        same = (str(state) == self.status.spectrum_state
                and _same_spectrum(frame, self.status.spectrum))
        self.status.spectrum = frame
        hz = getattr(frame, "peak_hz", None) if frame is not None else None
        self.status.peak_hz = float(hz) if isinstance(hz, (int, float)) and hz else None
        self.status.spectrum_state = str(state)
        if not same:
            self.changed.set()

    @property
    def last_input(self) -> float:
        """When the last knob/button event (or page change) came, on the UI clock: the render
        loop draws the frame after an input at the knob rate, other frames at the stream rate."""
        return self._last_input

    def set_mute(self, on: bool | str | None = MUTE_TOGGLE, seen: int | None = None) -> bool:
        """Main mute, sent at once (never queued behind other engine work). 'toggle' = the
        opposite of what the screen shows. `seen` (an unmute from a surface that LOOKED first:
        B4's press, a phone's hold): the mute_view() generation it saw; if the mute changed
        since, the unmute is refused (someone muted meanwhile) and the mute shown is returned.
        A mute is never refused."""
        if on is None or on == MUTE_TOGGLE:
            on = not self.displayed_muted()
        on = bool(on)
        if not on and seen is not None and seen != self.mute_view()[1]:
            log.warning("unmute refused: the mute changed since it was seen (generation %s, now %s)",
                        seen, self.mute_seq)
            self.show_hint(HINT_MUTE_CHANGED)
            return self.displayed_muted()
        self.mute_seq += 1
        self.state.muted = on
        self._mute_want = on
        log.info("mute %s (operator)", "ON" if on else "OFF")
        self._mark_dirty(urgent=on)
        self._changed()
        if self._mute_task is None or self._mute_task.done():
            try:
                self._mute_task = asyncio.get_running_loop().create_task(self._mute_sender(),
                                                                         name="k1000-mute")
            except RuntimeError:   # no loop (sync test): the next connect sync sends it
                pass
        return on

    def next_page(self) -> str:
        """B1 tap (D-013): SHOW <-> ALIGN, SETUP -> SHOW, PHONE -> SETUP (input_map.B1_TAP)."""
        return self.goto_page(B1_TAP.get(self.page, HOME_PAGE))

    def goto_page(self, page: str) -> str:
        """Show `page` (v0 names MAIN/LIVE are aliases of ALIGN/SHOW). Leaving ALIGN locks its
        E3/E4 again; a page change drops a pending SETUP confirm."""
        page = page_name(page)
        if page not in ALL_PAGES:
            log.warning("unknown page %r", page)
            return self.page
        if page != "ALIGN":
            self.focus.unlock_until = 0.0
        if page != self.page:
            self.focus.confirm = None
            self.focus.clear_pick()
        self._last_input = self._ui_clock()
        self.page = page
        self._changed()
        return page

    def ui_tick(self, now: float | None = None) -> None:
        """ALIGN and SETUP go back to SHOW after HOME_AFTER_S without a knob/button event (not
        while a SETUP confirm or choice is pending; PHONE and the Knob test stay where they are)."""
        now = self._ui_clock() if now is None else now
        if (self.page in AUTO_HOME_PAGES and now - self._last_input >= HOME_AFTER_S
                and not self.focus.confirm_now(now) and not self.focus.pick_now(now)):
            log.info("%s idle %.0f s: back to %s", self.page, now - self._last_input, HOME_PAGE)
            self.goto_page(HOME_PAGE)

    async def _ui_loop(self) -> None:
        while True:
            try:
                self.ui_tick()
            except Exception as e:   # noqa: BLE001
                self._log_once(("ui", type(e).__name__), logging.ERROR, "ui tick failed: %s", e)
            await asyncio.sleep(UI_TICK_S)

    def show_hint(self, text: str, seconds: float = HINT_S) -> None:
        self.focus.hint, self.focus.hint_until = text, time.monotonic() + seconds
        self._changed()

    def displayed_muted(self) -> bool:
        """The mute the screen shows: the engine's Main mute, else the state's."""
        return bool(self.status.muted) if self.status.muted is not None else bool(self.state.muted)

    def b2_muted(self) -> bool:
        """The mute B4 long acts against (it asks for the opposite).

        Normally the one the screen shows. But while K1000 ITSELF holds Main muted on its way to
        bringing it in (a boot ramp parked, a setup guard) and the operator has not muted, the
        screen says Main comes in by itself and that B4 keeps it muted: B4 then means MUTE (the
        operator's mute, which the ramp/guard honours), never an unmute into an engine that is
        not processing yet."""
        if (self._ramping or FADER_MAIN in self._guards) and not self._operator_muted():
            return False
        return self.displayed_muted()

    def mute_view(self) -> tuple[bool, int]:
        """(the mute B4 acts against = b2_muted(), its generation). The generation moves when
        the mute shown or b2_muted() changes between two calls, and on every set_mute(). B4's
        press and every phone (web.PhoneBridge) read it; cheap, call it as often as needed."""
        key = (self.displayed_muted(), self.b2_muted())
        if key != self._mute_seen:
            self._mute_seen = key
            self.mute_seq += 1
        return key[1], self.mute_seq

    def handle_event(self, event: Any) -> None:
        """A knob/button event -> input_map actions for the current page -> applied. Every event
        counts as input (the auto-home timer; the spectrum holds its next frame)."""
        self._last_input = self._ui_clock()
        rta = self.rta
        if rta is not None:
            try:
                rta.note_input()
            except Exception:   # noqa: BLE001 - the knobs never depend on the spectrum
                pass
        muted, seq = self.mute_view()
        self.focus.readonly = self.setup_readonly()
        for action in map_event(self.page, self.focus, event, self.controls,
                                muted=muted, mute_seq=seq, state=self.state):
            self.handle_action(action)
        self._changed()   # HWTEST log, held pushes, confirm and choice marks

    def handle_action(self, action: Any) -> None:
        """One input_map Action. UI-local ones (slot/item select, hints) stay in `focus`."""
        if apply_focus(self.focus, action):
            self._changed()
            return
        if isinstance(action, Nudge):
            self.nudge(action.field, action.detents, action.fine)
        elif isinstance(action, SetChoice):
            self.set_choice(action.field, action.value)
        elif isinstance(action, Toggle):
            # Console and older callers; the knobs send SetChoice since D-013.
            if action.field == F_LOWCUT_ON:
                self.set_lowcut(not self.state.lowcut_on)
            elif self.toggle(action.field, confirmed=True) and action.field == F_SPECTRUM_ON:
                on = bool(self.state.spectrum_on)
                self.show_hint(HINT_SPECTRUM_DISABLED if on and not self.spectrum_enabled
                               else ("SPECTRUM ON" if on else "SPECTRUM OFF"))
        elif isinstance(action, Mute):
            self.set_mute(action.on, seen=action.seen)
        elif isinstance(action, ResetLive):
            self.reset_live()
        elif isinstance(action, UndoReset):
            self.undo_reset()
        elif isinstance(action, NotchHelper):
            self.notch_helper()
        elif isinstance(action, SetupAction):
            fn = self.setup_actions.get(action.name)
            if fn is None:
                self.show_hint(f"{action.name.upper()}: NOT AVAILABLE")
            else:
                try:
                    fn()
                except Exception as e:   # noqa: BLE001 - a SETUP extra must not break the knobs
                    log.exception("setup action %s failed: %s", action.name, e)
                    self.show_hint(f"{action.name.upper()} FAILED (SEE LOG)")
        elif isinstance(action, NextPage):
            self.next_page()
        elif isinstance(action, GotoPage):
            self.goto_page(action.page)
        elif isinstance(action, PhoneToggle):
            self.phone_toggle()
        elif isinstance(action, PhoneQr):
            if self.phone is not None:
                self.phone.select_qr(action.delta)
                self._changed()
        else:
            log.debug("unhandled action %r", action)

    # ------------------------------------------------------------------ laptop access
    def attach_laptop(self, laptop: Any) -> None:
        """Wire SETUP > Laptop access (laptop.LaptopAccess): the SETUP action, the countdown
        pill ("LAPTOP OPEN 1:59", before the system pills) and the screen/hint updates."""
        self.laptop = laptop
        self.setup_actions[A_LAPTOP] = self.laptop_toggle
        if laptop.pill not in self.pill_sources:
            self.pill_sources.append(laptop.pill)
        laptop.on_change = self._laptop_changed
        self._laptop_changed()

    def laptop_toggle(self) -> bool | None:
        """SETUP > Laptop access: open (after its double push) when closed, close (one push)
        when open. The opening itself is root's (k1000-laptop.service): this only asks, and the
        screen follows what root reports. None = no laptop surface."""
        lap = self.laptop
        if lap is None:
            self.show_hint(HINT_LAPTOP_NA)
            return None
        if lap.state in ("open", "opening"):
            lap.close("box")
            self.show_hint(HINT_LAPTOP_CLOSED, LAPTOP_HINT_S)
            return False
        ok = lap.open()          # its on_change already put a refusal on the screen
        if ok:
            self.show_hint(HINT_LAPTOP_OPENING, LAPTOP_HINT_S)
        elif not self.focus.hint_now():
            self.show_hint(HINT_LAPTOP_NA, LAPTOP_HINT_S)
        return bool(ok)

    def _laptop_view(self) -> tuple[float, str]:
        lap = self.laptop
        if lap is None:
            return 0.0, "off"
        try:
            return round(float(lap.left_s()), 1), str(lap.state)
        except Exception as e:   # noqa: BLE001 - never break the screen
            self._log_once(("laptop", type(e).__name__), logging.ERROR, "laptop view: %s", e)
            return 0.0, "off"

    def _laptop_changed(self) -> None:
        """LaptopAccess moved (root opened/closed it, or the minute shown changed): closing
        becomes a single push while open, root's news becomes a hint, the screen redraws."""
        lap = self.laptop
        if lap is None:
            return
        state = getattr(lap, "state", "closed")
        if state == "open":
            self.focus.no_confirm.add(A_LAPTOP)
        else:
            self.focus.no_confirm.discard(A_LAPTOP)
        seen, self._laptop_seen = self._laptop_seen, state
        msg = lap.take_message() if hasattr(lap, "take_message") else ""
        if msg:
            self.show_hint(msg, LAPTOP_HINT_S)
        elif state == "open" and seen in ("opening", "unavailable", None):
            self.show_hint(HINT_LAPTOP_OPEN, LAPTOP_HINT_S)
        self._changed()

    # ------------------------------------------------------------------ system EQ (the tune)
    def system_eq(self) -> list[dict[str, float]]:
        """The 8 system PEQ bands as plain dicts (freq, gain, q)."""
        return [{"freq": b.freq, "gain": b.gain, "q": b.q} for b in self.state.sys_peq]

    def set_system_eq(self, bands: Any, source: str = "tune") -> list[dict[str, float]]:
        """The whole system EQ at once (the laptop's /tune page: an import, or its undo).

        `bands`: 8 mappings (or objects) with freq/gain/q. Every value goes through State.set
        (the system ranges; a missing or unreadable value keeps the current one), then ONE local
        edit: the worker sends click-safe parameter writes (never a pipeline change) and the
        state is saved at once, like a mute (a tune must survive a power cut). Returns the
        system EQ as it was before (the caller's undo)."""
        prev = self.system_eq()
        seq = list(bands) if isinstance(bands, (list, tuple)) else []
        for i in range(N_SYS_PEQ):
            b = seq[i] if i < len(seq) else None
            if b is None:
                continue
            for k in ("freq", "gain", "q"):
                v = b.get(k) if isinstance(b, dict) else getattr(b, k, None)
                if v is None:
                    continue
                try:
                    self.state.set(f"sys_peq.{i}.{k}", v)
                except (KeyError, ValueError, TypeError) as e:
                    log.warning("system EQ band %d %s=%r refused: %s", i + 1, k, v, e)
        now = self.system_eq()
        if now != prev:
            log.warning("system EQ set (%s): %s", source,
                        ", ".join(f"{b['freq']:g} Hz {b['gain']:+g} dB Q {b['q']:g}"
                                  for b in now if b["gain"] != 0.0) or "flat")
            self._local_edit(urgent=True)
        return prev

    @property
    def unsaved(self) -> bool:
        """True until state.json on the disk holds the current state (the tune page says
        'saved' then). Not just "no save pending": a save in flight (its write runs in a thread)
        is not on the disk yet."""
        if self._dirty_since is not None:
            return True
        try:
            text = json.dumps(self.persist_state().to_dict(), indent=2) + "\n"
        except Exception:   # noqa: BLE001 - cannot tell: not saved
            return True
        return text != self._saved_state_text

    def phone_toggle(self) -> bool | None:
        """SETUP > Phone, E1 push: phone control on/off. None = no phone surface."""
        if self.phone is None:
            self.show_hint("PHONE DISABLED (k1000.yaml / --no-phone)")
            return None
        on = bool(self.phone.toggle())
        self.show_hint("PHONE CONTROL ON" if on else "PHONE OFF · KNOBS ONLY")
        return on

    def _setup_allowed(self, field: str, confirmed: bool) -> bool:
        if field == "profile" and self.profile_pinned:
            self.show_hint(HINT_PINNED)
            return False
        if not (confirmed or self.displayed_muted()):
            self.show_hint(HINT_CONFIRM_SETUP)
            return False
        return True

    def _local_edit(self, urgent: bool = False) -> None:
        if self._undo is not None:
            self.undo_left()          # an audible live EQ edit ends the reset's undo, said so
        self._gen += 1
        self._mark_dirty(urgent=urgent)
        self._changed()
        self._kick.set()

    def _changed(self) -> None:
        self.version += 1
        self.changed.set()

    def _set_message(self, key: str, prio: int, text: str | None) -> None:
        if text:
            self._messages[key] = (prio, text)
        else:
            self._messages.pop(key, None)
        msg = max(self._messages.values())[1] if self._messages else ""
        alerts = [m for m in self._messages.values() if m[0] >= ALERT_PRIO]
        alert = max(alerts)[1] if alerts else ""
        if msg != self.status.message or alert != self.status.alert:
            self.status.message = msg
            self.status.alert = alert
            self._changed()

    def _note_profile_routing(self, profile: Profile) -> None:
        """A k1000.yaml input_map/output_map that could not be used as written is only a log
        line otherwise: a wrong program input (a mic to the PA) or an unrouted output must show."""
        bad = [w for w in profile.warnings
               if any(k in w for k in ("input_map", "output_map", "not routed"))]
        self._set_message("profile", 5, MSG_PROFILE_ROUTING if bad else None)

    def _log_once(self, key: Any, level: int, msg: str, *args: Any) -> None:
        if key not in self._logged:
            self._logged.add(key)
            log.log(level, msg, *args)

    # ------------------------------------------------------------------ engine plumbing
    def _expected(self, state: State, profile: Profile | None = None) -> dict[str, Any]:
        cfg = dc.generate(profile or self.profile, state)
        return self._transform(cfg) if self._transform else cfg

    def _pending_ops(self) -> list[Op]:
        """What the worker still has to send (mute excluded: it has its own path)."""
        return dc.diff_ops(_no_mute(self._applied), _no_mute(self.state))

    def _pending_keys(self) -> tuple[set[str], set[int]]:
        """Pointers and faders with local edits not sent yet (protected from adoption)."""
        pa, ps = dc.param_map(self._applied), dc.param_map(self.state)
        ptrs = {k for k, v in ps.items() if pa.get(k) != v}
        fa, fs = dc.fader_levels(self._applied), dc.fader_levels(self.state)
        return ptrs, {i for i in fs if fa.get(i) != fs[i]}

    def _recent_own(self) -> tuple[set[str], set[int]]:
        now = self._clock()
        return ({p for p, t in self._own_ptr.items() if now - t < OWN_WRITE_S},
                {i for i, t in self._own_fader.items() if now - t < OWN_WRITE_S})

    async def _send(self, ops: Iterable[Op]) -> None:
        """Send one batch in order: consecutive SetValues as one write (PatchConfig when several,
        so the engine never sits on a half-applied change), faders clamped to contract ranges."""
        group: list[SetValue] = []

        async def flush() -> None:
            if len(group) == 1:
                await self.client.set_value(group[0].pointer, group[0].value)
            elif group:
                await self.client.patch_config(dc.ops_to_patch(group))
            now = self._clock()
            for g in group:
                self._own_ptr[g.pointer] = now
            group.clear()

        for op in ops:
            if isinstance(op, SetValue):
                group.append(op)
                continue
            await flush()
            if isinstance(op, Fader):
                await self.client.set_fader(op.index, self._fader_db(op.index, op.db))
                self._own_fader[op.index] = self._clock()
            elif isinstance(op, FaderMute) and op.index in AUX_FADERS:
                await self._send_aux_mute(op.index, op.mute)
            elif isinstance(op, FaderMute):
                await self.client.set_fader_mute(op.index, op.mute)
                self._own_fader[op.index] = self._clock()
                if op.index == FADER_MAIN:
                    self.status.muted = op.mute
                    self._changed()
            elif isinstance(op, Rebuild):
                raise ValueError("a Rebuild cannot be sent as a batch")
        await flush()

    def _fader_db(self, idx: int, db: float) -> float:
        """Contract-clamped level; an Aux group lowered outside K1000 keeps its offset."""
        v = _clamp_fader(idx, db)
        off = self._aux_offset.get(idx, 0.0)
        if idx in AUX_FADERS and off < 0.0:
            v = max(AUX_FLOOR_DB, round(v + off, 3))
        return v

    async def _send_aux_mute(self, idx: int, mute: bool) -> None:
        """A group mute/unmute from a guard, a release or the boot ramp. Someone else's mute is
        never touched (a delay/polarity write under it is inaudible anyway). K1000's own guard
        is recorded on the marker BEFORE the mute and cleared AFTER the unmute."""
        if idx in self._aux_ext_mute:
            log.debug("Aux%d muted outside K1000: guard op %s skipped", idx, mute)
            return
        if mute:
            await self._set_guard(idx, True)
            await self.client.set_fader_mute(idx, True)
        else:
            await self.client.set_fader_mute(idx, False)
            await self._set_guard(idx, False)
        self._own_fader[idx] = self._clock()

    async def _set_guard(self, idx: int, on: bool) -> None:
        before = set(self._guards)
        if on:
            self._guards.add(idx)
        else:
            self._guards.discard(idx)
        if self._guards != before:
            await self._write_marker()

    async def _clear_guard_quietly(self, idx: int) -> None:
        """Best effort (also from a `finally:` after a cancel): if the marker write fails the
        engine keeps the record, which only makes the next sync re-check that fader."""
        try:
            await asyncio.wait_for(asyncio.shield(self._set_guard(idx, False)), 1.0)
        except BaseException:   # noqa: BLE001 - link down, cancelled again...
            self._guards.discard(idx)

    async def _write_marker(self) -> None:
        """Make Aux4 say what `_guards` says (written only when it differs)."""
        want = marker_db(self._guards)
        cur = self._marker_engine
        if cur is None or abs(cur - want) > MARKER_TOL_DB / 2:
            await self.client.set_fader(MARKER_FADER, want)
            self._marker_engine = want

    def _note_engine(self, est: Any) -> None:
        est = str(est)
        if est != self.status.engine:
            self.status.engine = est
            self._changed()

    async def _engine_running(self, epoch: int, *, held: bool = False) -> bool:
        """GetState reads Running (with `held`: twice, RUN_CHECK_S apart). Anything else means
        a fader change now would be applied as a step when audio resumes."""
        for k in range(2 if held else 1):
            if k:
                await self._sleep(RUN_CHECK_S)
                self._check_link(epoch)
            est = await self.client.get_state()
            self._note_engine(est)
            if str(est) != "Running":
                return False
        return True

    async def _wait_running(self, epoch: int) -> None:
        """Hold here (Main muted by the caller) until the engine processes audio. No timeout: a
        stalled capture (USB start-up, an M4 replug) can last; the screen says why meanwhile."""
        shown = False
        try:
            while True:
                self._check_link(epoch)
                if self._stopping:
                    raise _LinkChanged("controller stopping")
                if await self._engine_running(epoch, held=True):
                    if shown:
                        log.info("engine running again: bringing Main in")
                    return
                if not shown:
                    shown = True
                    log.warning("engine %s (no audio flowing): Main held muted until it runs",
                                self.status.engine)
                    self._set_message("wait", 5, MSG_HELD_MUTED)
                await self._sleep(RUN_WAIT_S)
        finally:
            if shown:
                self._set_message("wait", 5, None)

    def _check_link(self, epoch: int) -> None:
        if epoch != self._epoch or not self.client.connected:
            raise _LinkChanged("engine link changed")

    # ------------------------------------------------------------------ mute path
    async def _mute_sender(self) -> None:
        """Send the latest operator mute until it is in the engine (or the link is down: the
        next connect sync takes it from there)."""
        while self._mute_want is not None:
            want = self._mute_want
            if not self.client.connected:
                return
            if want is False and self._main_guard_holds():
                # A setup guard holds Main muted AT THE SHOW LEVEL until the engine runs: an
                # unmute now would come in as a step when audio resumes (README, Volume
                # control). The operator's wish is recorded (state.muted False); the guard
                # unmutes once the engine runs, then this sends it (a no-op by then). Stopping:
                # the guard's own finish, or the next k1000 (Main's marker bit), brings Main in.
                if self._stopping:
                    return
                await asyncio.sleep(0.02)
                continue
            try:
                await self.client.set_fader_mute(FADER_MAIN, want)
            except CdspUnavailable:
                return
            except CdspError as e:
                log.error("mute %s refused by the engine: %s", want, e)
                if self._mute_want == want:
                    self._mute_want = None
                return
            self._own_fader[FADER_MAIN] = self._clock()
            self._applied.muted = want
            self.status.muted = want
            if self._mute_want == want:
                self._mute_want = None
            self._changed()

    def _main_guard_holds(self) -> bool:
        """A setup guard (not a boot ramp: Main is parked low there) holds Main muted."""
        return FADER_MAIN in self._guards and not self._ramping

    # ------------------------------------------------------------------ apply worker
    async def _worker(self) -> None:
        while True:
            await self._kick.wait()
            self._kick.clear()
            try:
                await self._apply_pending()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("apply worker error")

    async def _apply_pending(self) -> None:
        while self._engine_ready and self.client.connected and not self._stopping:
            ops = self._pending_ops()
            if not ops:
                return
            if any(isinstance(o, SetValue) and dc._guard_group(o.pointer) is not None for o in ops):
                await self._settle_edits()
            async with self._engine_lock:
                if not self._engine_ready or self._stopping:
                    return
                if not await self._apply_once():
                    return    # refused or link lost: wait for the next edit / reconnect

    async def _settle_edits(self) -> None:
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        gen = self._gen
        while loop.time() - t0 < GUARD_SETTLE_MAX_S:
            await self._sleep(GUARD_SETTLE_S)
            if self._gen == gen:
                return
            gen = self._gen

    async def _apply_once(self) -> bool:
        """One plan from `_applied` to the state. False when it failed (no busy retry loop)."""
        base = self._applied.copy()
        target = self.state.copy()
        target.muted = base.muted
        epoch = self._epoch
        plan = dc.smooth_ops(base, target)
        rebuild = next((op for _w, b in plan for op in b if isinstance(op, Rebuild)), None)
        self._busy = True
        try:
            if rebuild is not None:
                await self._rebuild(base, target, rebuild.reason, epoch)
            else:
                if any(isinstance(op, FaderMute) and op.index in AUX_FADERS
                       for _w, b in plan for op in b):
                    # A delay/polarity guard: who owns each group mute right now? (the 1 Hz
                    # poll may be up to a second old; a GUI mute must survive the guard)
                    self._note_aux_mutes(await self.client.get_faders())
                await self._run_plan(plan, epoch)
                applied = target.copy()
                applied.muted = self._applied.muted
                self._applied = applied
            return True
        except CdspUnavailable as e:
            log.warning("engine link lost while applying (%s); re-sync on reconnect", e)
            self._engine_ready = False
            if rebuild is not None:
                # e.g. a profile switch to an absent device: the engine exited and comes back
                # from active.yml, which still holds the old setup (saved only on success).
                self._revert_setup(base, "SETUP INTERRUPTED")
            return False
        except (CdspError, ValueError) as e:
            log.error("engine refused an edit (%s); re-reading the engine", e)
            self.show_hint("ENGINE REFUSED THE CHANGE")
            await self._resync_from_engine()
            return False
        finally:
            self._busy = False
            self._changed()

    async def _run_plan(self, plan: dc.Plan, epoch: int) -> None:
        try:
            for wait, batch in plan:
                if wait > 0:
                    await self._sleep(wait)
                self._check_link(epoch)
                await self._send(batch)
        except BaseException:
            # A guard cut short must not leave a group muted while the engine plays.
            release = dc.plan_release_ops(plan)
            if release and self.client.connected:
                try:
                    await asyncio.wait_for(asyncio.shield(self._send(release)), 1.0)
                except BaseException:
                    pass
            raise

    async def _resync_from_engine(self) -> None:
        """After a refused write: the engine's values win (the refused edit is dropped)."""
        try:
            live = await self.client.get_config_json()
            faders = await self.client.get_faders()
        except CdspUnavailable as e:
            log.warning("cannot re-read the engine (%s); re-sync on reconnect", e)
            self._engine_ready = False
            return
        except CdspError as e:
            log.warning("cannot re-read the engine (%s); the next poll adopts", e)
            return
        skip = {FADER_MAIN} if self._mute_want is not None else set()
        self._applied = dc.state_from_config(live, faders, self._applied)
        self.state = dc.state_from_config(live, faders, self.state, skip_faders=skip)
        self._mark_dirty()
        self._changed()

    async def _rebuild(self, base: State, target: State, reason: str, epoch: int) -> None:
        """A setup action: regenerate + upload the whole config, Main muted around it."""
        profile_change = target.profile != self.profile.name
        new_profile = self.profile
        if profile_change:
            try:
                new_profile = await asyncio.to_thread(self._profile_loader, target.profile)
            except Exception as e:   # noqa: BLE001 - ProfileError, probe failure...
                log.error("profile %s cannot be loaded: %s", target.profile, e)
                self._revert_setup(base, f"PROFILE {target.profile.upper()} FAILED")
                return
        expected = self._expected(target, new_profile)
        try:
            live: Any = await self.client.get_config_json()
        except CdspNoConfig:
            live = None
        cfg = expected if (profile_change or live is None) else dc.with_live_devices(expected, live)
        log.warning("setup action: %s -> pipeline rebuild%s", reason,
                    " + device restart" if profile_change else "")
        self._set_message("setup", 4, "APPLYING SETUP…")
        try:
            ok = await self._guarded_upload(cfg, ramp_after=profile_change, epoch=epoch)
        finally:
            self._set_message("setup", 4, None)
        if not ok:
            self._revert_setup(base, "SETUP FAILED")
            return
        if profile_change:
            self.profile = new_profile
            self.status.profile = new_profile.name
            self.status.device = DEVICE_NAMES.get(new_profile.name, new_profile.name)
            self._note_profile_routing(new_profile)
            self._refresh_device()
        # Faders: only what the guard/ramp actually sent (tracked in _applied); the worker sends
        # any level change made in the same edit on its next pass.
        applied = target.copy()
        applied.muted = self._applied.muted
        applied.master_db = self._applied.master_db
        applied.balance_db = self._applied.balance_db
        self._applied = applied
        if profile_change:
            self.save_now()      # an engine restart must come back on the new profile
        else:
            self._mark_dirty()
        self._kick.set()         # fader changes made meanwhile

    def _revert_setup(self, base: State, hint: str) -> None:
        for f in REBUILD_FIELDS:
            setattr(self.state, f, getattr(base, f))
        self.show_hint(hint)
        self._mark_dirty()

    def _operator_muted(self) -> bool:
        return bool(self.state.muted) or self._mute_want is True

    async def _guarded_upload(self, cfg: dict[str, Any], *, ramp_after: bool, epoch: int) -> bool:
        """Upload a whole config with Main muted around it (if it was audible). The unmute waits
        until the engine processes audio (a stalled engine would apply it as a step on resume);
        after a device change (`ramp_after`) Main comes back with the boot ramp instead. Main's
        marker bit is set while the guard holds it: a k1000 killed inside the guard is followed
        by one that ramps Main in, instead of taking the guard's mute for the operator's."""
        audible = not self._applied.muted and self.status.muted is not True
        finished = not audible       # Main back where the operator wants it
        handed = False               # the boot ramp took over Main (and its marker bit)
        if audible:
            await self._set_guard(FADER_MAIN, True)
            await self.client.set_fader_mute(FADER_MAIN, True)
            self._own_fader[FADER_MAIN] = self._clock()
            self._applied.muted = True
            self.status.muted = True
        ok = False
        try:
            if audible:
                await self._sleep(dc.GUARD_WAIT_S)
            try:
                await self.client.set_config_yaml(dc.to_yaml(cfg), timeout=UPLOAD_TIMEOUT_S)
                ok = True
            except CdspUnavailable:
                raise    # engine gone: it comes back muted, the connect sync decides
            except (CdspError, ValueError) as e:
                log.error("config upload refused: %s", e)
            if audible:
                if self._operator_muted():
                    finished = True          # the operator's mute took over: stays muted
                elif ramp_after and ok:
                    handed = True
                    await self._boot_ramp(self.state, epoch)
                else:
                    await self._wait_running(epoch)
                    if not self._operator_muted():
                        await self.client.set_fader_mute(FADER_MAIN, False)
                        self._own_fader[FADER_MAIN] = self._clock()
                        self._applied.muted = False
                        self.status.muted = False
                    finished = True
        except BaseException:
            if audible and not finished and not handed:
                finished = await self._finish_main_guard()
            raise
        finally:
            if audible and not handed:
                if finished:
                    await self._clear_guard_quietly(FADER_MAIN)
                # else: the engine keeps the Main bit; the next connect sync ramps Main in
            self._changed()
        return ok

    async def _finish_main_guard(self) -> bool:
        """A Main guard cut short (stop, cancel): unmute now if the engine is processing and the
        operator wants sound. True when Main is where the operator wants it."""
        if self._operator_muted():
            return True
        if not self.client.connected:
            return False

        async def go() -> bool:
            if str(await self.client.get_state()) != "Running":
                return False
            await self.client.set_fader_mute(FADER_MAIN, False)
            self._own_fader[FADER_MAIN] = self._clock()
            self._applied.muted = False
            self.status.muted = False
            return True

        try:
            return await asyncio.wait_for(asyncio.shield(go()), 1.0)
        except BaseException:   # noqa: BLE001 - from a cancelled task's except: never raise here
            return False

    def _ramp_ops(self, batch: list[Op]) -> list[Op]:
        """The operator stays in charge of a ramp: a mute pressed meanwhile drops its unmute, a
        master turned down caps every later step."""
        ops: list[Op] = []
        for op in batch:
            if (isinstance(op, FaderMute) and op.index == FADER_MAIN and not op.mute
                    and self._operator_muted()):
                continue
            if isinstance(op, Fader) and op.index == FADER_MAIN:
                op = Fader(FADER_MAIN, min(op.db, _clamp_fader(FADER_MAIN, self.state.master_db)))
            ops.append(op)
        return ops

    def _note_main(self, ops: Iterable[Op]) -> None:
        for op in ops:
            if isinstance(op, Fader) and op.index == FADER_MAIN:
                self._applied.master_db = op.db
            elif isinstance(op, FaderMute) and op.index == FADER_MAIN:
                self._applied.muted = op.mute

    def boot_ramp_s_for(self, master_db: float, start_db: float | None = None) -> float:
        """boot_ramp_s, lengthened so that no ramp step exceeds BOOT_STEP_MAX_DB (a k1000.yaml
        typo such as 0.3 instead of 3 must not give 20 dB jumps)."""
        master = _clamp_fader(FADER_MAIN, master_db)
        first = self.boot_gain_db if start_db is None else start_db
        start = min(RANGES["master_db"].clamp(first, dc.BOOT_GAIN_DB), master)
        step_s = dc.VOLUME_RAMP_MS / 1000.0
        need = math.ceil((master - start) / BOOT_STEP_MAX_DB - 1e-9) * step_s if master > start else 0.0
        return max(self.boot_ramp_s, round(need, 6))

    def _ramp_plan(self, target: State, start_db: float) -> tuple[list[Op], dc.Plan, float]:
        """(park batch, the rest, Main's park level) of a boot ramp from `start_db`."""
        plan = dc.boot_ramp_plan(target, start_db, self.boot_ramp_s_for(target.master_db, start_db))
        park = plan[0][1]
        start = next((op.db for op in park if isinstance(op, Fader) and op.index == FADER_MAIN),
                     start_db)
        return park, plan[1:], start

    async def _boot_ramp(self, target: State, epoch: int, start_db: float | None = None) -> None:
        """boot_ramp_plan, with the operator and the engine in charge.

        - The engine must be PROCESSING (Running) for the unmute and for every step: CamillaDSP
          applies a fader change made while Stalled/Paused/Starting at once when audio resumes,
          so a ramp into a stalled engine would start the PA at the full master. Until it runs,
          Main stays muted at the start level; if it stops mid-ramp, Main is muted and parked at
          the boot gain again and the ramp restarts from there once audio flows.
        - A mute pressed during the ramp drops its unmute; a master turned down caps the steps.
        - No step exceeds BOOT_STEP_MAX_DB (`boot_ramp_s_for`).
        - Main's marker bit is set for the duration: a k1000 killed mid-ramp is followed by one
          that ramps in again, instead of adopting the half-way level as the operator's master.
        `start_db`: resume from this (audible) level instead of the boot gain (never below it).
        """
        first = self.boot_gain_db if start_db is None else max(self.boot_gain_db, start_db)
        park, rest, start = self._ramp_plan(target, first)
        self._ramping = True
        self.status.ramping = True
        self._set_message("ramp", 3, "RAMPING IN")
        done = False
        try:
            await self._set_guard(FADER_MAIN, True)
            ops = self._ramp_ops(park)
            await self._send(ops)
            self._note_main(ops)
            i = 0
            while i < len(rest):
                wait, batch = rest[i]
                if wait > 0:
                    await self._sleep(wait)
                self._check_link(epoch)
                if not await self._engine_running(epoch, held=(i == 0)):
                    if self._applied.muted is False:
                        park, rest, start = self._ramp_plan(target, self.boot_gain_db)
                        log.warning("engine %s mid-ramp: Main muted and parked at %.1f dB; the "
                                    "ramp restarts when audio flows", self.status.engine, start)
                        park_again: list[Op] = [FaderMute(FADER_MAIN, True), Fader(FADER_MAIN, start)]
                        await self._send(park_again)
                        self._note_main(park_again)
                    await self._wait_running(epoch)
                    i = 0
                    continue
                ops = self._ramp_ops(batch)
                await self._send(ops)
                self._note_main(ops)
                i += 1
            self._applied.balance_db = target.balance_db
            done = True
        finally:
            self._ramping = False
            self.status.ramping = False
            self._set_message("ramp", 3, None)
            if done:
                await self._clear_guard_quietly(FADER_MAIN)
            # else: the engine keeps Main's bit; the next connect sync ramps in again

    # ------------------------------------------------------------------ connect sync
    async def _on_connect(self, client: Any) -> None:
        """Every (re)connection: fresh engine -> push + ramp; playing engine -> adopt."""
        self._epoch += 1
        epoch = self._epoch
        self._engine_ready = False
        self.status.connected = True
        self._changed()
        while epoch == self._epoch and not self._stopping:
            try:
                async with self._engine_lock:
                    if self._stopping or epoch != self._epoch:
                        return
                    self._syncing = True
                    try:
                        await self._sync(epoch)
                    finally:
                        self._syncing = False
            except CdspUnavailable as e:
                log.warning("connect sync interrupted (%s)", e)
                return
            except Exception as e:   # noqa: BLE001 - CdspError (config refused), a bug...
                # The engine stays as it is (muted if it just started): retry, never guess.
                self._log_once(("sync", type(e).__name__, str(e)), logging.ERROR,
                               "connect sync failed (%s: %s); engine left as it is, retrying "
                               "every %g s", type(e).__name__, e, SYNC_RETRY_S)
                self._set_message("engine", 6, "ENGINE SYNC FAILED (SEE LOG)")
                await asyncio.sleep(SYNC_RETRY_S)
                continue
            if epoch == self._epoch:
                self._engine_ready = True
                self._set_message("engine", 6, None)
                self._kick.set()
            self._changed()
            return

    async def _on_disconnect(self, client: Any) -> None:
        self._epoch += 1
        self._engine_ready = False
        self._marker_engine = None
        self.status.connected = False
        self.status.engine = None
        self.status.muted = None
        self.status.load = None
        self.status.levels = [METER_FLOOR_DB] * 4
        self._changed()

    async def _sync(self, epoch: int) -> None:
        version = await self.client.get_version()
        self.status.version = str(version)
        if not str(version).startswith("5."):
            self._log_once(("version", version), logging.WARNING,
                           "engine version %s: K1000 v0 is built for CamillaDSP 5.0.0-beta.1", version)
        try:
            live: Any = await self.client.get_config_json()
        except CdspNoConfig:
            live = None
        faders = await self.client.get_faders()
        self._note_engine(await self.client.get_state())
        main_vol, main_mute = _fader(faders, FADER_MAIN)
        self.status.muted = main_mute
        marker, _ = _fader(faders, MARKER_FADER)
        self._marker_engine = marker
        guards = decode_marker(marker)
        # What K1000 left in flight on THIS engine (none on a restarted one: no marker).
        self._guards = set(guards or ())
        at_start = main_vol is not None and abs(main_vol - ENGINE_START_DB) <= FRESH_TOL_DB
        if (guards is not None and FADER_MAIN in guards and main_mute is False and main_vol is not None
                and main_vol >= _clamp_fader(FADER_MAIN, self.state.master_db) - dc.FADER_TOL_DB):
            # The ramp/guard had in fact completed (only clearing its record was lost, e.g. a
            # link blip at that moment): nothing to resume, no dip.
            self._guards.discard(FADER_MAIN)
            guards = guards - {FADER_MAIN}
        if live is None or (guards is None and main_mute is True and at_start):
            log.info("engine %s (Main %s dB muted, no K1000 marker): pushing the state, ramping in",
                     "has no config" if live is None else "freshly started", main_vol)
            await self._sync_fresh(live, faders, epoch)
        elif guards is not None and FADER_MAIN in guards:
            log.warning("engine Main %s dB%s was left in a K1000 guard/ramp (k1000 stopped inside "
                        "it): adopting the rest, then ramping Main in again", main_vol,
                        " muted" if main_mute else "")
            await self._sync_running(live, faders, epoch, resume_main=True)
        else:
            managed = guards is not None
            if not managed and main_mute is True:
                log.warning("engine muted at Main %s dB without K1000's marker: someone muted it "
                            "(not a fresh start at %g dB); adopted as muted, not ramped in",
                            main_vol, ENGINE_START_DB)
            else:
                log.info("engine already playing (Main %s dB%s%s): adopting, no ramp", main_vol,
                         " muted" if main_mute else "", "" if managed else ", not brought in by K1000")
            await self._sync_running(live, faders, epoch, managed=managed)
        self._check_link(epoch)
        await self._write_marker()

    async def _sync_fresh(self, live: Any, faders: Any, epoch: int) -> None:
        # A fresh engine's Aux faders are its defaults (0 dB): no offsets to keep. An Aux mute on
        # it is someone's (the defaults are unmuted): kept.
        self._aux_offset = {i: 0.0 for i in AUX_FADERS}
        self._aux_ext_mute.clear()
        self._note_aux_mutes(faders)
        target = self.state.copy()
        expected = self._expected(target)
        if live is None or live.get("title") != expected.get("title"):
            log.warning("engine config %r is not K1000's %r: uploading (engine muted)",
                        None if live is None else live.get("title"), expected.get("title"))
            await self.client.set_config_yaml(dc.to_yaml(expected), timeout=UPLOAD_TIMEOUT_S)
        else:
            drift = dc.config_drift(live, expected)
            if drift.devices or drift.rebuild:
                log.warning("engine config differs (%s): uploading K1000's (engine muted)",
                            "; ".join(drift.devices + drift.rebuild)[:300])
                await self.client.set_config_yaml(dc.to_yaml(expected), timeout=UPLOAD_TIMEOUT_S)
            elif drift.fixes:
                log.info("engine config: %d value(s) pushed from the saved state", len(drift.fixes))
                await self.client.patch_config(dc.ops_to_patch(drift.fixes))
        self._check_link(epoch)
        main_vol, main_mute = _fader(faders, FADER_MAIN)
        if main_mute is not True:    # an engine without config: make the ramp start from muted
            await self._send([FaderMute(FADER_MAIN, True)])
        applied = target.copy()
        applied.profile = self.profile.name      # what was just verified/uploaded
        applied.muted = True
        applied.master_db = _clamp_fader(FADER_MAIN, main_vol if main_vol is not None else self.boot_gain_db)
        applied.balance_db = 0.0
        self._applied = applied
        self._mute_want = None   # the boot plan honours state.muted itself
        await self._boot_ramp(target, epoch)

    async def _sync_running(self, live: Any, faders: Any, epoch: int, *, managed: bool = True,
                            resume_main: bool = False) -> None:
        """An engine that was already playing. `managed` = it carries K1000's marker; if not
        (someone unmuted it before K1000 synced), its Aux faders are the engine defaults (0/0),
        not a balance: they are not adopted, the saved balance is pushed instead.
        `resume_main`: the marker says K1000 left Main in a guard or the boot ramp: Main's
        mute/level are K1000's transient values, not the operator's; the saved state's Main is
        brought back with the boot ramp."""
        # Aux at the engine's defaults (0 dB, unmuted) on an engine K1000 did not bring in: not
        # a balance, push the saved one. Moved by someone: theirs, same rules as a managed engine.
        aux_defaults = not managed and all(
            v is not None and abs(v) <= dc.FADER_TOL_DB and m is False
            for v, m in (_fader(faders, i) for i in AUX_FADERS))
        adopt_skip = {FADER_SUBS, FADER_TOPS} if aux_defaults else set()
        if resume_main:
            adopt_skip.add(FADER_MAIN)
        title = live.get("title")
        expected = self._expected(self.state)
        other = _k1000_profile(title)
        if title != expected.get("title") and other is not None and other != self.profile.name:
            # k1000.yaml switched profile and k1000 was restarted (README): the engine must follow.
            log.warning("engine runs %r, k1000 runs profile %s: switching the engine (device "
                        "restart, about 1 s of silence, then the boot ramp)", title, self.profile.name)
            await self._adopt(live, faders, connect=True, extra_skip=adopt_skip)
            self._aux_drift(faders, set(), reset=aux_defaults)  # offsets/mutes for the ramp's Aux ops
            if resume_main and self.status.muted is not True:
                await self._send([FaderMute(FADER_MAIN, True)])
                self._applied.muted = True
            await self._guarded_upload(self._expected(self.state), ramp_after=True, epoch=epoch)
            if resume_main:
                await self._boot_ramp(self.state, epoch)
            self._applied.profile = self.profile.name
            for f in ("mono_subs", "dev_audition"):
                setattr(self._applied, f, getattr(self.state, f))
            return
        await self._reconcile(live, faders, connect=True, adopt_skip=adopt_skip,
                              drift_skip={FADER_MAIN} if resume_main else set(),
                              aux_reset=aux_defaults)
        if resume_main:
            main_vol, main_mute = _fader(faders, FADER_MAIN)
            if main_mute is not None:
                self._applied.muted = main_mute
            # Audible half-way up a ramp: continue from there (no dip); muted: from the boot gain.
            await self._boot_ramp(self.state, epoch,
                                  start_db=main_vol if main_mute is False else None)

    # ------------------------------------------------------------------ adoption
    async def _adopt(self, live: Any, faders: Any, *, connect: bool,
                     extra_skip: set[int] | frozenset[int] = frozenset()) -> State:
        """Adopt the engine's owned values into the state (local unsent edits win). Returns the
        engine's view (what it holds now), which also becomes `_applied`. `extra_skip`: faders
        whose engine value is not meaningful (never adopted, in either view)."""
        recent_p, recent_f = self._recent_own()
        pend_p, pend_f = self._pending_keys()
        main_skip = {FADER_MAIN} if (self._mute_want is not None or self._ramping) else set()
        main_skip |= set(extra_skip)
        # Both views skip the same unreliable reads (our own writes of the last OWN_WRITE_S, Main
        # while a mute is in flight or a ramp runs). If only the state skipped them, the engine
        # view would differ from it there, which reads as a pending local edit, and the worker
        # would push the old value back over a phone move: fighting the phone.
        engine_view = dc.state_from_config(live, faders, self._applied, skip_pointers=recent_p,
                                           skip_faders=recent_f | main_skip)
        new = dc.state_from_config(live, faders, self.state, skip_pointers=recent_p | pend_p,
                                   skip_faders=recent_f | pend_f | main_skip)
        for f in ("mono_subs", "dev_audition"):     # a setup edit not applied yet also wins
            if getattr(self.state, f) != getattr(self._applied, f):
                setattr(new, f, getattr(self.state, f))
        _vol, engine_muted = _fader(faders, FADER_MAIN)
        if connect and self._mute_want is False:
            if FADER_MAIN in extra_skip or engine_muted is False:
                # Already as asked (or Main is K1000's to ramp in): nothing left to send. Left
                # set, it would keep Main out of adoption and settle() busy for good.
                self._mute_want = None
            elif engine_muted:
                # An unmute pressed while the link was down never reached the engine: drop it.
                self._mute_want = None
                new.muted = True
                self.show_hint(HINT_STILL_MUTED)
        if new.to_dict() != self.state.to_dict():
            diff = changed_fields(self.state, new)
            log.info("adopted from the engine: %s",
                     ", ".join(f"{k} {a!r}->{b!r}" for k, (a, b) in list(diff.items())[:12]))
            if new.master_db != self.state.master_db:
                _log_master(self.state.master_db, new.master_db, "adoption")
            self.state = new
            self._mark_dirty()
            self._changed()
        self._applied = engine_view
        return engine_view

    async def _reconcile(self, live: Any, faders: Any, *, connect: bool,
                         adopt_skip: set[int] | frozenset[int] = frozenset(),
                         drift_skip: set[int] | frozenset[int] = frozenset(),
                         aux_reset: bool = False) -> None:
        """Adopt, then push back what the engine should hold but does not (fixed parameters,
        clamped values, a restored pipeline, the Aux faders, a hidden Main above range), in the
        safe direction only: cuts and mutes made outside K1000 are kept and shown.

        `adopt_skip`: faders whose engine value is not meaningful (not adopted). `drift_skip`:
        faders not pushed back either. `aux_reset`: the engine's Aux faders are its defaults
        (an engine K1000 did not bring in): the balance macro is pushed as is."""
        recent_p, recent_f = self._recent_own()
        pend_p, pend_f = self._pending_keys()
        main_skip = {FADER_MAIN} if (self._mute_want is not None or self._ramping) else set()
        engine_view = await self._adopt(live, faders, connect=connect, extra_skip=adopt_skip)

        skip_f = recent_f | pend_f | main_skip | set(drift_skip)
        # Who owns each Aux mute is settled first: a fix below may run a group guard.
        aux_ops, aux_held = self._aux_drift(faders, skip_f, reset=aux_reset)

        expected = self._expected(engine_view)
        wrong = self.wrong_devices(live, expected) if self.status.device_present is True else []
        if not wrong:
            self._device_seen = 0
        elif not connect and await self._maybe_repair_devices(live, expected, wrong):
            return    # devices + pipeline back from the profile; the next poll re-reconciles
        drift = dc.config_drift(live, expected, skip_pointers=recent_p)
        held: list[str] = []
        if drift.rebuild and wrong:
            # A pipeline restore keeps the live devices: useless there (and refused when the
            # channel counts differ). The device repair puts both back.
            self._set_message("drift", 2, MSG_PIPELINE_CHANGED)
        elif drift.rebuild:
            await self._maybe_restore(live, expected, drift.rebuild, connect)
        else:
            self._rebuild_seen = 0
            self._set_message("drift", 2, None)
            held += await self._apply_fixes(live, drift.fixes)

        # Main: only ever pulled down or muted (dspconfig.fader_drift); the Aux groups: above.
        fops = dc.fader_drift(faders, self.state, skip_faders=skip_f | set(AUX_FADERS)) + aux_ops
        held += aux_held
        self._report_drift(drift.devices, held)
        if fops:
            log.info("faders pushed back: %s", fops)
            await self._send(fops)
            for op in fops:
                if isinstance(op, Fader) and op.index == FADER_MAIN:
                    self._applied.master_db = op.db
                elif isinstance(op, Fader):
                    self._applied.balance_db = self.state.balance_db
                elif isinstance(op, FaderMute) and op.index == FADER_MAIN:
                    self._applied.muted = op.mute
        if connect and self._mute_want is True and (self._mute_task is None or self._mute_task.done()):
            self._mute_task = asyncio.get_running_loop().create_task(self._mute_sender(),
                                                                     name="k1000-mute")
        if self._pending_ops():
            self._kick.set()

    # ------------------------------------------------------------------ Aux group policy
    def _note_aux_mutes(self, faders: Any, skip: Iterable[int] = ()) -> list[int]:
        """Who owns each Aux mute. K1000's when its guard is recorded (in `_guards` or the
        engine's marker: a guard cut short, a k1000 killed inside it): returned, to release.
        Anyone else's (CamillaGUI's Aux panel): remembered in `_aux_ext_mute`, never released."""
        bits = (decode_marker(_fader(faders, MARKER_FADER)[0]) or frozenset()) | self._guards
        skip = set(skip)
        release: list[int] = []
        for i in AUX_FADERS:
            if i in skip:
                continue
            _v, mute = _fader(faders, i)
            if mute is True:
                if i in bits:
                    self._aux_ext_mute.discard(i)
                    release.append(i)
                elif i not in self._aux_ext_mute:
                    self._aux_ext_mute.add(i)
                    log.warning("%s group (Aux%d) muted outside K1000: kept (K1000 never unmutes "
                                "it on its own)", AUX_NAMES[i], i)
            elif mute is False:
                if i in self._aux_ext_mute:
                    log.info("%s group (Aux%d) unmuted outside K1000", AUX_NAMES[i], i)
                self._aux_ext_mute.discard(i)
                self._guards.discard(i)     # nothing left to release (the marker follows)
        return release

    def _aux_drift(self, faders: Any, skip: Iterable[int], *, reset: bool = False
                   ) -> tuple[list[Op], list[str]]:
        """Aux1/Aux2 against the balance macro (Aux1 = +b/2, Aux2 = -b/2), never raising a level
        or releasing a mute K1000 did not set:
        - a mute: released only when it is K1000's guard (`_note_aux_mutes`), else kept;
        - a level ABOVE the macro (a one-sided or common-mode raise): pulled down to it;
        - a level BELOW it: kept as an offset that later balance moves carry along;
        - `reset` (engine defaults, not brought in by K1000): the macro is pushed as is.
        Returns (ops, held descriptions for the screen)."""
        skip = set(skip)
        lv = dc.fader_levels(self.state)
        ops: list[Op] = []
        for g in self._note_aux_mutes(faders, skip):
            log.warning("Aux%d was left muted by a K1000 guard (cut short, or k1000 killed inside "
                        "it): releasing it", g)
            ops.append(FaderMute(g, False))
        for i in AUX_FADERS:
            vol, _m = _fader(faders, i)
            if i in skip or vol is None:
                continue
            target = lv[i]
            if reset or vol > target + dc.FADER_TOL_DB:
                self._aux_offset[i] = 0.0
                if abs(vol - target) > dc.FADER_TOL_DB:
                    ops.append(Fader(i, target))
            elif vol < target - dc.FADER_TOL_DB:
                off = round(vol - target, 3)
                if abs(off - self._aux_offset[i]) > dc.FADER_TOL_DB:
                    log.warning("%s group (Aux%d) at %.2f dB, %.2f dB under K1000's balance: kept",
                                AUX_NAMES[i], i, vol, -off)
                self._aux_offset[i] = off
            else:
                self._aux_offset[i] = 0.0
        held = [f"{AUX_NAMES[i]} GROUP MUTED" for i in AUX_FADERS if i in self._aux_ext_mute]
        held += [f"{AUX_NAMES[i]} GROUP {self._aux_offset[i]:+.1f} dB" for i in AUX_FADERS
                 if self._aux_offset[i] < 0.0]
        return ops, held

    # ------------------------------------------------------------------ config value fixes
    def _fix_plan(self, live: Any, fixes: list[SetValue]) -> tuple[dc.Plan, list[SetValue], list[str]]:
        """Split `config_drift` fixes into what may be pushed back and what is kept.

        Kept (shown, never reverted by K1000): anything that would RAISE the level or unmute: a
        Gain filter muted outside K1000, a Gain/Biquad gain or Volume limit below K1000's value
        (e.g. sub_trim -10 dB or out_trim_0 muted to measure one speaker group at the tune).
        Pushed: the rest, click-safe like a knob move: gains/limits stepped down (0.1 dB per
        step on a Gain filter, which the engine applies as an instant step; 0.5 dB on a Biquad),
        frequencies and Q in 1/12-octave steps, everything else in one write, under the group
        guard when it belongs to the subs/tops chain (a delay or unit change clicks).
        Returns (plan, pushed fixes, held descriptions)."""
        filters = live.get("filters") if isinstance(live, dict) else None
        filters = filters if isinstance(filters, dict) else {}
        by_filter: dict[str, list[SetValue]] = {}
        for f in fixes:
            parts = f.pointer.strip("/").split("/")
            name = parts[1] if len(parts) == 4 and parts[0] == "filters" else ""
            by_filter.setdefault(name, []).append(f)
        held: list[str] = []
        pushed: list[SetValue] = []
        tracks: list[tuple[str, float, float, bool, float]] = []   # ptr, from, to, log, step
        direct: list[SetValue] = []
        for name, fx in by_filter.items():
            lf = filters.get(name) if isinstance(filters.get(name), dict) else {}
            ftype = lf.get("type")
            lpar = lf.get("parameters") if isinstance(lf.get("parameters"), dict) else {}
            want = {f.pointer.rsplit("/", 1)[-1]: f.value for f in fx}
            why = self._raise_reason(name, ftype, lpar, want)
            if why:
                held.append(why)
                continue
            pushed += fx
            unit_change = any(k in want for k in ("scale", "delay_unit"))
            for f in fx:
                key = f.pointer.rsplit("/", 1)[-1]
                a, b = _num(lpar.get(key)), _num(f.value)
                if not unit_change and a is not None and b is not None:
                    if key in ("gain", "limit"):
                        tracks.append((f.pointer, a, b, False, 0.1 if ftype == "Gain" else 0.5))
                        continue
                    if key in ("freq", "q") and a > 0 and b > 0:
                        tracks.append((f.pointer, a, b, True, 1.0 / 12.0))
                        continue
                direct.append(f)
        plan: dc.Plan = []
        if tracks:
            n = 1
            for _p, a, b, is_log, step in tracks:
                span = abs(math.log2(b / a)) if is_log else abs(b - a)
                n = max(n, math.ceil(span / step - 1e-9))
            n = min(n, 100)
            for k in range(1, n + 1):
                t = k / n
                batch: list[Op] = []
                for ptr, a, b, is_log, _s in tracks:
                    v = b if k == n else round(a * (b / a) ** t if is_log else a + (b - a) * t, 3)
                    batch.append(SetValue(ptr, v))
                plan.append((dc.STEP_WAIT_S if plan else 0.0, batch))
        plain = [f for f in direct if dc._guard_group(f.pointer) is None]
        guarded = [f for f in direct if dc._guard_group(f.pointer) is not None]
        if plain:
            plan.append((dc.STEP_WAIT_S if plan else 0.0, list(plain)))
        if guarded:
            groups = sorted({g for g in (dc._guard_group(f.pointer) for f in guarded) if g is not None})
            plan.append((dc.STEP_WAIT_S if plan else 0.0, [FaderMute(g, True) for g in groups]))
            plan.append((dc.GUARD_WAIT_S, list(guarded)))
            plan.append((dc.STEP_WAIT_S, [FaderMute(g, False) for g in groups]))
        return plan, pushed, held

    @staticmethod
    def _raise_reason(name: str, ftype: Any, lpar: dict[str, Any], want: dict[str, Any]) -> str:
        """Why pushing `want` back over the live parameters would raise the level ("" = it
        would not)."""
        if want.get("mute") is False and lpar.get("mute") is True:
            return f"{name} MUTED"
        if ftype == "Gain" and isinstance(want.get("gain"), (int, float)):
            live_db = dc._gain_db(lpar)
            if live_db is not None and live_db < float(want["gain"]) - 0.01:
                return f"{name} {live_db:+.1f} dB"
        if ftype == "Biquad":
            g, w = _num(lpar.get("gain")), _num(want.get("gain"))
            if g is not None and w is not None and g < w - 0.01:
                return f"{name} GAIN {g:+.1f} dB"
        if ftype == "Volume":
            g, w = _num(lpar.get("limit")), _num(want.get("limit"))
            if g is not None and w is not None and g < w - 0.01:
                return f"{name} LIMIT {g:+.1f} dB"
        return ""

    async def _apply_fixes(self, live: Any, fixes: list[SetValue]) -> list[str]:
        """Push back the safe fixes as a click-safe plan; return the held ones (for the screen)."""
        if not fixes:
            self._fix_key, self._fix_repeats = None, 0
            return []
        plan, pushed, held = self._fix_plan(live, fixes)
        if held:
            self._log_once(("held", tuple(held)), logging.WARNING,
                           "set outside K1000 and kept (K1000 never raises a level or unmutes on "
                           "its own): %s", "; ".join(held))
        if not pushed:
            self._fix_key, self._fix_repeats = None, 0
            return held
        key = tuple(sorted((f.pointer, repr(f.value)) for f in pushed))
        self._fix_repeats = self._fix_repeats + 1 if key == self._fix_key else 1
        self._fix_key = key
        if self._fix_repeats > FIX_MAX_REPEATS:
            self._log_once(("fix", key), logging.ERROR,
                           "engine keeps differing on %s: giving up re-sending", [f.pointer for f in pushed])
            self._set_message("drift", 2, "CONFIG DRIFT (SEE LOG)")
            return held
        log.info("config values pushed back (%d step(s)): %s", len(plan),
                 ", ".join(f"{f.pointer}={f.value!r}" for f in pushed))
        await self._run_plan(plan, self._epoch)
        return held

    def _report_drift(self, devices: list[str], held: list[str]) -> None:
        drift = list(devices) + list(held)
        if drift != self.status.drift:
            self.status.drift = drift
            self._changed()
        if devices:
            self._log_once(("devices", tuple(devices)), logging.WARNING,
                           "engine devices differ from K1000's (not fixed while audio runs on "
                           "the profile's card; fixed at the next engine start, e.g. a power "
                           "cycle): %s", "; ".join(devices))
            self._set_message("devices", 1, MSG_DEVICES_CHANGED)
        else:
            self._set_message("devices", 1, None)
        if held:
            text = (f"{held[0].upper()} {OUTSIDE}" if len(held) == 1
                    else f"{len(held)} CUTS/MUTES {OUTSIDE}")
            self._set_message("held", 1, text)
        else:
            self._set_message("held", 1, None)

    def wrong_devices(self, live: Any, expected: dict[str, Any]) -> list[str]:
        """Why the engine cannot be playing the profile's program to the profile's outputs: a
        capture/playback side of another type, on another ALSA card, or with another channel
        count (e.g. a CamillaGUI page loaded while the engine was down holds the GUI's default
        config, hw:0 2/2 ch, and its Apply moves the engine there: on m4, card 0 is the
        HiFiBerry). [] when it may well be right (other device options only, or cannot tell)."""
        out: list[str] = []
        ld = dc._dict(dc._as_cfg(live).get("devices"))
        ed = dc._dict(expected.get("devices"))
        for side in ("capture", "playback"):
            lv, ev = dc._dict(ld.get(side)), dc._dict(ed.get(side))
            if not ev:
                continue
            if lv.get("type") != ev.get("type"):
                out.append(f"{side} type {lv.get('type')!r} != {ev.get('type')!r}")
                continue
            if ev.get("type") != "Alsa":
                continue
            ldev, edev = lv.get("device"), ev.get("device")
            if isinstance(ldev, str) and isinstance(edev, str) and ldev != edev:
                if self.same_card(ldev, edev) is False:
                    out.append(f"{side} on {ldev!r}, not {edev!r}")
                    continue
            if lv.get("channels") != ev.get("channels"):
                out.append(f"{side} channels {lv.get('channels')!r} != {ev.get('channels')!r}")
        return out

    async def _maybe_repair_devices(self, live: Any, expected: dict[str, Any],
                                    wrong: list[str]) -> bool:
        """The engine runs on the wrong card/channels while the profile's card is present: the
        PA plays nothing (or the wrong input) whatever the knobs do, and a pipeline restore
        keeps the live devices, so it cannot help. Put the profile's whole config back (device
        restart, Main muted around it, then the boot ramp): already silent, so this is the safe
        direction. Second look first, at most once per RESTORE_MIN_INTERVAL_S. True = done."""
        self._device_seen += 1
        if self._device_seen < 2:
            return False
        now = self._clock()
        if now - self._last_repair < RESTORE_MIN_INTERVAL_S:
            return False
        self._last_repair = now
        log.warning("engine is not on the profile's devices (%s): restoring %s's config (device "
                    "restart, Main muted, then the boot ramp)", "; ".join(wrong), self.profile.name)
        self._set_message("devices", 1, "RESTORING ENGINE DEVICES")
        ok = await self._guarded_upload(_keep_cuts(expected, live), ramp_after=True,
                                        epoch=self._epoch)
        if ok:   # what was uploaded was generated from the engine view (`_applied`)
            self._device_seen = 0
            self._applied.profile = self.profile.name
        return True

    async def _maybe_restore(self, live: Any, expected: dict[str, Any], reasons: list[str],
                             connect: bool) -> None:
        """The running pipeline is not K1000's (a CamillaGUI structure edit): put it back, Main
        muted around the upload, after a second look and at most once per interval."""
        self._rebuild_seen += 1
        if not connect and self._rebuild_seen < 2:
            return
        now = self._clock()
        if now - self._last_restore < RESTORE_MIN_INTERVAL_S:
            self._set_message("drift", 2, MSG_PIPELINE_CHANGED)
            return
        self._last_restore = now
        log.warning("pipeline differs from K1000's (%s): restoring it (Main muted around the "
                    "upload)", "; ".join(reasons)[:300])
        cfg = _keep_cuts(dc.with_live_devices(expected, live), live)
        ok = await self._guarded_upload(cfg, ramp_after=False, epoch=self._epoch)
        self._set_message("drift", 2, None if ok else MSG_PIPELINE_CHANGED)
        if ok:
            self._rebuild_seen = 0

    # ------------------------------------------------------------------ polling
    async def _poll_loop(self) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except CdspUnavailable:
                pass
            except Exception as e:   # noqa: BLE001 - the loop must survive anything
                self._log_once(("poll", type(e).__name__, str(e)), logging.ERROR,
                               "poll failed: %s: %s", type(e).__name__, e)
            await asyncio.sleep(self.poll_s)

    async def poll_once(self) -> None:
        """Engine state/load/clip, device presence, then (if idle) adoption + drift repair."""
        self._refresh_device()
        if not self.client.connected:
            if self.status.engine is not None or self.status.connected:
                self.status.engine = None
                self.status.connected = False
                self.status.muted = None
                self._changed()
            return
        est = await self.client.get_state()
        load = await self.client.get_processing_load()
        clipped = await self.client.get_clipped_samples()
        reason = ""
        if est in ENGINE_DOWN_STATES:
            reason = str(await self.client.get_stop_reason())
        self._update_clip_counter(clipped)
        if (est, load, reason) != (self.status.engine, self.status.load, self.status.stop_reason):
            self.status.engine, self.status.load, self.status.stop_reason = est, load, reason
            self._changed()
        if not self._engine_ready or self._syncing or self._engine_lock.locked():
            if not self._syncing:
                _v, m = _fader(await self.client.get_faders(), FADER_MAIN)
                if m != self.status.muted and not self._ramping:
                    self.status.muted = m
                    self._changed()
            return
        async with self._engine_lock:     # settle()/stop() wait for this work too
            if not self._engine_ready or self._stopping:
                return
            try:
                live = await self.client.get_config_json()
            except CdspNoConfig:
                log.warning("engine lost its config: uploading K1000's")
                await self._sync(self._epoch)
                return
            faders = await self.client.get_faders()
            self._marker_engine = _fader(faders, MARKER_FADER)[0]
            _v, m = _fader(faders, FADER_MAIN)
            if self._mute_want is None and m != self.status.muted:
                self.status.muted = m
                self._changed()
            await self._reconcile(live, faders, connect=False)
            await self._write_marker()

    def _refresh_device(self) -> None:
        if self._device_probe is None:
            present: bool | None = True
        else:
            vals = []
            for dev in {self.profile.capture_device, self.profile.playback_device}:
                try:
                    vals.append(self._device_probe(dev))
                except Exception:   # noqa: BLE001
                    vals.append(None)
            present = False if any(v is False for v in vals) else (True if all(v is True for v in vals) else None)
        if present != self.status.device_present:
            if present is False:
                log.warning("audio device %s not present", self.profile.playback_device)
            self.status.device_present = present
            self._changed()

    def _update_clip_counter(self, clipped: int) -> None:
        if self._last_clipped is not None and clipped > self._last_clipped:
            hold = self._peak_hold
            hot = [i for i in range(4) if hold[i] >= -0.5] or [max(range(4), key=lambda i: hold[i])]
            until = self._clock() + CLIP_HOLD_S
            for i in hot:
                self._clip_until[i] = until
            log.warning("engine clipped %d samples", clipped - self._last_clipped)
        self._last_clipped = clipped
        self.status.clipped_samples = clipped
        self._peak_hold = [METER_FLOOR_DB] * 4

    async def _meter_loop(self) -> None:
        while True:
            try:
                await self.meter_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:   # noqa: BLE001
                self._log_once(("meter", type(e).__name__), logging.WARNING, "meter read failed: %s", e)
            await asyncio.sleep(self.meter_s)

    async def meter_once(self) -> None:
        """Output peaks since the last read -> logical meters + clip latch; the program input
        channels' capture peaks -> IN CLIP latch (the M4's line inputs clip in the ADC, before
        any K1000 gain, and nothing on the output side shows it)."""
        cap: Any = None
        if not self.client.connected:
            levels = [METER_FLOOR_DB] * 4
        else:
            try:
                peaks = await self.client.command("GetPlaybackSignalPeakSinceLast")
            except (CdspUnavailable, CdspError):
                peaks = None
            levels = logical_levels(self.profile, self._applied.dev_audition, peaks)
            try:
                cap = await self.client.command("GetCaptureSignalPeakSinceLast")
            except (CdspUnavailable, CdspError):
                cap = None
        now = self._clock()
        for i, lv in enumerate(levels):
            self._peak_hold[i] = max(self._peak_hold[i], lv)
            if lv >= CLIP_DBFS:
                self._clip_until[i] = now + CLIP_HOLD_S
        if isinstance(cap, (list, tuple)):
            for ch in set(self.profile.input_map):
                v = cap[ch] if 0 <= ch < len(cap) else None
                if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= IN_CLIP_DBFS:
                    if now >= self._in_clip_until:
                        log.warning("input clipping (capture channel %d at %.2f dBFS): the "
                                    "source must come down", ch, v)
                    self._in_clip_until = now + CLIP_HOLD_S
        clip = [now < t for t in self._clip_until]
        in_clip = now < self._in_clip_until
        # What the screen can show: -60..0 dBFS in METER_STEP_DB steps. Store every value, wake
        # the render loop only when the picture changes.
        key = (tuple(round(max(METER_SHOWN_FLOOR_DB, min(0.0, lv)) / METER_STEP_DB)
                     for lv in levels), tuple(clip), in_clip)
        self.status.levels, self.status.clip, self.status.in_clip = levels, clip, in_clip
        if key != self._meter_key:
            self._meter_key = key
            self._changed()

    # ------------------------------------------------------------------ persistence
    def _mark_dirty(self, urgent: bool = False) -> None:
        now = self._clock()
        if self._dirty_since is None:
            self._dirty_since = now
        self._last_change = now
        if urgent:
            self._urgent_save = True
        self._persist_kick.set()

    async def _persist_loop(self) -> None:
        while True:
            await self._persist_kick.wait()
            self._persist_kick.clear()
            while self._dirty_since is not None:
                now = self._clock()
                due = min(self._last_change + self.persist_debounce_s,
                          self._dirty_since + self.persist_max_delay_s)
                if self._urgent_save:
                    due = min(due, self._last_save + URGENT_SAVE_GAP_S)
                if now < due:
                    # Woken early by an urgent edit (mute, master down) arriving meanwhile.
                    self._persist_kick.clear()
                    try:
                        await asyncio.wait_for(self._persist_kick.wait(), due - now)
                    except (TimeoutError, asyncio.TimeoutError):
                        pass
                    continue
                self._urgent_save = False
                self._last_save = self._clock()
                if await self._save_async():
                    self._save_failures = 0
                    break
                # A transient write error (ENOSPC, EIO, a short EROFS on the SD card) must not
                # leave the last edits (a feedback notch) only in memory until the next edit:
                # still dirty, retried with a backoff.
                self._save_failures += 1
                delay = SAVE_RETRY_S[min(self._save_failures, len(SAVE_RETRY_S)) - 1]
                if self._dirty_since is None:
                    self._dirty_since = self._clock()
                log.warning("state not saved; retrying in %g s (attempt %d)", delay,
                            self._save_failures + 1)
                await asyncio.sleep(delay)

    def persist_state(self) -> State:
        """What goes to disk: the state, with the pipeline structure actually running."""
        st = self.state.copy()
        for f in ("mono_subs", "dev_audition"):
            setattr(st, f, getattr(self._applied, f))
        st.profile = self.profile.name
        return st.clamped()

    def _save_items(self) -> list[tuple[Path, str, str, bool]] | None:
        """(path, data, bookkeeping attr, compare-with-disk-first) for each file whose content
        changed since the last save; None when the content cannot be built."""
        self._dirty_since = None
        st = self.persist_state()
        try:
            text = json.dumps(st.to_dict(), indent=2) + "\n"
            cfg = self._expected(st)
            # The YAML text only when the config changed: MASTER, the balance and the mute are
            # faders, not in active.yml, so a MASTER-down save (every 0.25 s) costs only the
            # state JSON (PERF-3).
            key = json.dumps(cfg, default=repr)       # ~0.3 ms; a snapshot, never aliased
            if self._yaml_cache is not None and self._yaml_cache[0] == key:
                yml = self._yaml_cache[1]
            else:
                yml = dc.to_yaml(cfg)
                self._yaml_cache = (key, yml)
        except Exception as e:   # noqa: BLE001 - a generate() bug must not kill the loop
            if ("build", type(e).__name__, str(e)) not in self._logged:
                self._logged.add(("build", type(e).__name__, str(e)))
                log.exception("cannot build the files to save: %s", e)
            self._set_message("save", 7, "CANNOT SAVE STATE")
            return None
        return [(Path(path), data, attr, getattr(self, attr) is None)
                for path, data, attr in ((self.paths.state, text, "_saved_state_text"),
                                         (self.paths.active_config, yml, "_saved_cfg_text"))
                if getattr(self, attr) != data]

    def _write_items(self, items: list[tuple[Path, str, str, bool]]) -> list[OSError | None]:
        """The disk part (fsync can take tens of ms on an SD card): run off the event loop.
        One lock serialises writers, so the newest content is always renamed last."""
        out: list[OSError | None] = []
        with self._write_lock:
            for path, data, _attr, compare in items:
                old: str | None = None
                try:
                    if compare:
                        old = path.read_text(encoding="utf-8")
                        if old == data:
                            out.append(None)
                            continue
                except (OSError, UnicodeDecodeError):
                    pass
                if old is not None and path == self.paths.active_config:
                    # First overwrite of active.yml by this process: keep what the engine
                    # booted from (the tune, should the new content be wrong).
                    try:
                        atomic_write(path.with_name(path.name + ".prev"), old)
                    except OSError as e:
                        log.warning("cannot keep %s.prev: %s", path.name, e)
                try:
                    atomic_write(path, data)
                    out.append(None)
                except OSError as e:
                    out.append(e)
        return out

    def _saved(self, items: list[tuple[Path, str, str, bool]], results: list[OSError | None]) -> bool:
        ok = True
        for (path, data, attr, _c), err in zip(items, results):
            if err is None:
                setattr(self, attr, data)
            else:
                ok = False
                self._log_once(("save", str(path), err.errno), logging.ERROR,
                               "cannot write %s: %s", path, err)
        self._set_message("save", 7, None if ok else "CANNOT SAVE STATE")
        if not ok and self._dirty_since is None:   # still unsaved: the persist loop retries
            self._dirty_since = self._clock()
            self._persist_kick.set()
        return ok

    async def _save_async(self) -> bool:
        items = self._save_items()
        if items is None:
            return False
        if not items:
            return True
        return self._saved(items, await asyncio.to_thread(self._write_items, items))

    def save_now(self) -> bool:
        """Write state.json + active.yml now (atomically, only what changed). Never raises.
        Blocking: for start, stop and a profile switch; the debounced saves use a thread."""
        items = self._save_items()
        if items is None:
            return False
        return self._saved(items, self._write_items(items)) if items else True
