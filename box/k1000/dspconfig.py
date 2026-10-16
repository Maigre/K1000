# SPDX-License-Identifier: GPL-3.0-or-later
"""CamillaDSP v5 config from (Profile, State), and the minimal live edits between two States.

Why it is shaped like this (docs/03-v0-architecture.md §3):
- The pipeline has fixed slots. Every show-time action is a parameter write into an existing
  filter (`SetValue`, JSON pointer into the config) or a ramped fader move (`Fader`,
  `FaderMute`). Only setup actions (mono subs, profile, dev audition) change a mixer or a device,
  and those come out as one `Rebuild(reason)`: the caller regenerates and uploads the whole
  config, accepting a possible click.
- `param_map(state)` is the single list of owned parameters: `generate`, `diff_ops`,
  `state_from_config` and `config_drift` all derive from it, so they cannot disagree.
- Every value is clamped to `state.RANGES` before it is emitted, whoever built the State.
- A live band switched OUT (D-014) is written as 0 dB (`state.effective_gain`): a gain move,
  smoothed like a gain edit, never a pipeline change; its freq and Q stay where they are.
- Delays reach the engine as whole samples (`subsample: false` rounds anyway): a write that
  would not change the sample count is never emitted, because every Delay write rebuilds the
  delay line and clicks (measured).
- Faders live outside the config: K1000 owns Main (master/mute, GUI moves adopted), Aux1/Aux2
  (balance macro + the delay/polarity guard mute). `fader_drift` keeps the engine's faders on the
  state, `boot_ramp_plan` brings audio in after an engine (re)start.

Optional blocks (2026-10-16, after the first rig run; `State.aux_mode`, `loud_on`, `deharsh_on`):
each is in the config ONLY while switched on, so with all three off `generate` writes byte for
byte what f2671b0 wrote (tests/fixtures/dspconfig-f2671b0). Switching one is a structure change
(`Rebuild`, like mono subs); its levels/frequencies are owned parameters (`param_map`, so a
`SetValue`, click-free). The FILTER and PROCESSOR definitions depend on the State alone, so
`param_map(state)` never names one the config lacks; the ROUTING (mixers, pipeline) also depends
on the Profile: an AUX switched on where the profile has no spare capture channel (dev) is defined
but nothing is routed through it (the config plays exactly as with AUX off).
- AUX IN (`aux_mode` "mic" | "line"): the capture channels not in `input_map` (M4: IN1, IN2) join
  the `input` mixer as A1/A2; they get `aux_gain` (+ `aux_hp` for a mic), then the `aux_mix` mixer
  sums them into program L/R AFTER the program's input trim and BEFORE the room EQ, so the AUX
  gets the system/live EQ and de-harsh, not the trim. The input chain step is split in two around
  it (`in_trim`, then the EQ), only while an AUX is routed.
- De-harsh (`deharsh_on`): `dh_split` 2 -> 6 (L, R, then two copies of each), `dh_band` band-pass
  on the copies, `dh_comp` compresses copies 4/5 (monitoring their sum), `dh_sum` 6 -> 2 adds
  L - band + compressed band. At its centre the band-pass has 0 dB gain and 0 phase, so this is a
  peaking cut as deep as the gain reduction; under the threshold the gain is exactly 1 and the
  sum is x - B + B, a pass-through. Placed after the input EQ, before `split`.
- Sub loudness (`loud_on`): `sub_loudness` (Loudness on Main) in the subs chain after the low-pass.
"""
from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass
from typing import Any, Callable, Iterable, NamedTuple, Union

import yaml

from .profiles import LOGICAL_NAMES, Profile
from .state import (
    AUX_MIC_HP_HZ, AUX_MIC_HP_Q, AUX_MIC_PAN_DB, DEHARSH_ATTACK_MS, DEHARSH_Q, DEHARSH_RATIO,
    DEHARSH_RELEASE_MS, LOUD_HIGH_BOOST_DB, LOUD_LOW_FREQ_HZ, LOWCUT_OFF_HZ, LOWCUT_Q, N_LIVE_PEQ,
    N_OUT, N_SYS_PEQ, RANGES, SAMPLE_MS, SAMPLERATE, SUB_LOWPASS_OFF_HZ, SUB_LOWPASS_Q, PeqBand,
    ShelfBand, State, delay_samples, effective_gain,
)

CHUNKSIZE = 256
TARGET_LEVEL = 256
# Engine fader ramp (devices.volume_ramp_time_ms for Main, ramp_time_ms of the Aux1/Aux2 Volume
# filters). The engine restarts the whole ramp on every new target, so a turning knob trails
# the hand by about one ramp: 100 ms (was 300 ms) lets master and balance answer the hand and
# still declicks a 0.5 dB step (docs/08-knob-precision.md row 3, D-009). It is also the mute
# and unmute fade, the boot ramp's step time and, + 50 ms, the delay/polarity guard dip.
VOLUME_RAMP_MS = 100.0
# Aux faders carry +-b/2 of the balance macro; the Volume filter `limit` caps the GAIN they apply
# there. It does not cap the stored fader value (GetFaders can read +20 while +6 is applied), so
# `fader_drift` still re-sends the state's levels.
AUX_LIMIT_DB = RANGES["balance_db"].max / 2.0
# Written as devices.volume_limit: caps the Main fader's applied gain while K1000's config is the
# active one. A CamillaGUI config Apply can lift it (that is a device restart, reported by
# `config_drift().devices`); a Main value above range is then pulled down by `fader_drift`.
MAIN_LIMIT_DB = RANGES["master_db"].max
# Engine boot level (camilladsp -m -g-40, contract §2) and the ramp to the saved master.
BOOT_GAIN_DB = -40.0
BOOT_RAMP_S = 3.0
# Aux1 = -Aux2 within this is a balance setting (adopted); anything else is reverted.
BALANCE_TOL_DB = 0.05
FADER_TOL_DB = 0.01
# An OUT live band reads back as 0 dB; a gain further from 0 than this was set outside K1000.
OUT_GAIN_TOL_DB = 0.001

FADER_MAIN, FADER_SUBS, FADER_TOPS = 0, 1, 2

SYS_PEQ: tuple[str, ...] = tuple(f"sys_peq_{i}" for i in range(1, N_SYS_PEQ + 1))
LIVE_PEQ: tuple[str, ...] = tuple(f"live_peq_{i}" for i in range(1, N_LIVE_PEQ + 1))
OUT_TRIM: tuple[str, ...] = tuple(f"out_trim_{i}" for i in range(N_OUT))
PROGRAM_EQ: tuple[str, ...] = (*SYS_PEQ, *LIVE_PEQ, "live_lowshelf", "live_highshelf")
INPUT_CHAIN: tuple[str, ...] = ("in_trim", *PROGRAM_EQ)
SUB_CHAIN: tuple[str, ...] = ("sub_trim", "sub_lowpass", "sub_delay", "sub_fader")
TOP_CHAIN: tuple[str, ...] = ("top_trim", "top_lowcut", "top_delay", "top_fader")
# The mixers every K1000 config has; the optional ones below exist only with their feature.
MIXERS: tuple[str, ...] = ("input", "split", "output")
# Optional blocks (module docstring): names are fixed, like every other slot.
AUX_GAIN, AUX_HP, AUX_MIX = "aux_gain", "aux_hp", "aux_mix"
DH_SPLIT, DH_BAND, DH_COMP, DH_SUM = "dh_split", "dh_band", "dh_comp", "dh_sum"
SUB_LOUDNESS = "sub_loudness"
# Channels after `dh_split`: 0/1 program L/R, 2/3 the band copies subtracted, 4/5 the band copies
# compressed (and monitored: their SUM is the compressor's level, +6 dB for a centred source).
DH_CHANNELS = 6
DH_BAND_CHANNELS: tuple[int, ...] = (2, 3, 4, 5)
DH_COMP_CHANNELS: tuple[int, ...] = (4, 5)


# --------------------------------------------------------------------------- ops
@dataclass(frozen=True)
class SetValue:
    """Write one config value: websocket SetConfigValue {pointer, value}."""

    pointer: str
    value: Any


@dataclass(frozen=True)
class Fader:
    """Set a fader volume in dB (0 Main, 1 Aux1 subs, 2 Aux2 tops): SetFaderVolume, ramped."""

    index: int
    db: float


@dataclass(frozen=True)
class FaderMute:
    """Set a fader's mute: SetFaderMute (ramped by the engine)."""

    index: int
    mute: bool


@dataclass(frozen=True)
class Rebuild:
    """The pipeline structure changes: regenerate the config and upload it whole."""

    reason: str


Op = Union[SetValue, Fader, FaderMute, Rebuild]


def pointer(*parts: str) -> str:
    """RFC 6901 JSON pointer from raw path segments."""
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def _unpointer(ptr: str) -> list[str]:
    if not ptr.startswith("/"):
        raise ValueError(f"not a JSON pointer: {ptr!r}")
    return [p.replace("~1", "/").replace("~0", "~") for p in ptr[1:].split("/")]


def _pp(filter_name: str, key: str) -> str:
    return pointer("filters", filter_name, "parameters", key)


def _proc_pp(processor_name: str, key: str) -> str:
    return pointer("processors", processor_name, "parameters", key)


def op_to_command(op: Op) -> dict[str, Any] | None:
    """The v5 websocket message for an op (None for Rebuild: the caller uploads a config)."""
    if isinstance(op, SetValue):
        return {"command": "SetConfigValue", "pointer": op.pointer, "value": op.value}
    if isinstance(op, Fader):
        return {"command": "SetFaderVolume", "fader": op.index, "value": op.db}
    if isinstance(op, FaderMute):
        return {"command": "SetFaderMute", "fader": op.index, "value": op.mute}
    return None


def ops_to_patch(ops: Iterable[Op]) -> dict[str, Any]:
    """Merge SetValue ops into one partial config for `PatchConfig`.

    One patch = one validation and one apply in the engine, so a multi-value change (reset live,
    a delay unit + value fix) never passes through a half-applied intermediate state.
    """
    patch: dict[str, Any] = {}
    for op in ops:
        if not isinstance(op, SetValue):
            continue
        parts = _unpointer(op.pointer)
        if any(p.isdigit() for p in parts):
            raise ValueError(f"cannot patch list element {op.pointer}")
        node = patch
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = op.value
    return patch


# --------------------------------------------------------------------------- owned values
def effective_lowcut_hz(s: State) -> float:
    return s.lowcut_hz if s.lowcut_on else LOWCUT_OFF_HZ


def effective_sub_lowpass_hz(s: State) -> float:
    return s.sub_lowpass_hz if s.sub_lowpass_on else SUB_LOWPASS_OFF_HZ


def delay_on_grid(ms: Any) -> float:
    """The ms value K1000 writes for a delay: a whole number of samples (6 digits, so the
    engine's round-to-nearest gives back exactly that count)."""
    return round(delay_samples(ms) * SAMPLE_MS, 6)


def aux_on(state: State) -> bool:
    """AUX IN switched on (mic or line). Whether anything is routed also needs a spare capture
    channel in the profile (`aux_inputs`); the AUX filters exist either way."""
    return getattr(state, "aux_mode", "off") in ("mic", "line")


def spare_capture_channels(profile: Profile) -> tuple[int, ...]:
    """Capture channels that do not carry the program (`input_map`), in order. M4 (program on
    LINE IN 3/4): (0, 1) = IN1, IN2. dev (HiFiBerry, 2 channels, both program): ()."""
    l, r = profile.input_map
    return tuple(c for c in range(profile.capture_channels) if c not in (l, r))


def aux_inputs(profile: Profile, aux_mode: str) -> tuple[int, ...]:
    """The capture channels the AUX takes, as (A1[, A2]); () = no AUX routing (off, or no spare).

    The first two spares, in both modes: a mic uses A1 only (A2 rides along to `aux_mix`, which
    drops it), so mic <-> line changes the AUX steps but not the `input` mixer. Line needs two:
    with a single spare it degrades to a mono line on A1, centred like the mic, without the
    mic's high-pass. Any further spare stays padded into L at linear 0 (`_input_mixer`)."""
    if aux_mode not in ("mic", "line"):
        return ()
    return spare_capture_channels(profile)[:2]


def param_map(state: State) -> dict[str, Any]:
    """Every config value K1000 owns, as {JSON pointer: value}, from the clamped state.

    Delays are snapped to whole samples, so two states whose delays round to the same sample
    count map to the same value and `diff_ops` emits nothing (no needless delay-line rebuild).
    The optional blocks' values are owned only while their feature is on (module docstring):
    `aux_gain` gain (aux_mode "mic"/"line"), `dh_band` freq + `dh_comp` threshold (deharsh_on),
    `sub_loudness` reference_level + low_boost (loud_on). Kept in the state while off, written
    to nothing.
    """
    s = state.clamped()
    m: dict[str, Any] = {_pp("in_trim", "gain"): s.input_trim_db}
    bands: list[tuple[str, PeqBand | ShelfBand]] = [
        *zip(SYS_PEQ, s.sys_peq), *zip(LIVE_PEQ, s.live_peq),
        ("live_lowshelf", s.live_lowshelf), ("live_highshelf", s.live_highshelf),
    ]
    for name, b in bands:
        m[_pp(name, "freq")] = b.freq
        m[_pp(name, "gain")] = effective_gain(b)      # an OUT live band plays 0 dB (D-014)
        m[_pp(name, "q")] = b.q
    m[_pp("sub_trim", "inverted")] = s.sub_inverted
    m[_pp("sub_lowpass", "freq")] = effective_sub_lowpass_hz(s)
    m[_pp("sub_delay", "delay")] = delay_on_grid(s.sub_delay_ms)
    m[_pp("top_trim", "inverted")] = s.top_inverted
    m[_pp("top_lowcut", "freq")] = effective_lowcut_hz(s)
    m[_pp("top_delay", "delay")] = delay_on_grid(s.top_delay_ms)
    for name, g in zip(OUT_TRIM, s.out_trim_db):
        m[_pp(name, "gain")] = g
    if aux_on(s):
        m[_pp(AUX_GAIN, "gain")] = s.aux_gain_db
    if s.deharsh_on:
        m[_pp(DH_BAND, "freq")] = s.deharsh_hz
        m[_proc_pp(DH_COMP, "threshold")] = s.deharsh_thresh_db
    if s.loud_on:
        m[_pp(SUB_LOUDNESS, "reference_level")] = s.loud_ref_db
        m[_pp(SUB_LOUDNESS, "low_boost")] = s.loud_boost_db
    return m


def fader_levels(state: State) -> dict[int, float]:
    """Target fader volumes: Main = master, Aux1 (subs) = +b/2, Aux2 (tops) = -b/2."""
    s = state.clamped()
    half = round(s.balance_db / 2.0, 3)
    return {FADER_MAIN: s.master_db, FADER_SUBS: half, FADER_TOPS: -half if half else 0.0}


def fader_ops(state: State) -> list[Op]:
    """Ops that put every owned fader where `state` says, in one batch.

    Aux1/Aux2 levels AND unmutes (K1000 never leaves a group muted outside a running guard, so
    this also clears a guard mute left by a plan cut short), then Main level, then Main mute.
    For a re-apply while the engine kept playing at about these levels. NOT for bringing audio
    in after an engine (re)start: it would unmute at the saved master within one fader ramp,
    skipping the contract's slow ramp-in; use `boot_ramp_plan` for that. For a reconnect where
    the levels may differ, prefer `state_from_config` + `fader_drift` (never raises Main).
    """
    lv = fader_levels(state)
    return [Fader(FADER_SUBS, lv[FADER_SUBS]), Fader(FADER_TOPS, lv[FADER_TOPS]),
            FaderMute(FADER_SUBS, False), FaderMute(FADER_TOPS, False),
            Fader(FADER_MAIN, lv[FADER_MAIN]), FaderMute(FADER_MAIN, state.clamped().muted)]


def boot_ramp_plan(state: State, start_db: float = BOOT_GAIN_DB, ramp_s: float = BOOT_RAMP_S) -> "Plan":
    """Bring audio in after the ENGINE (re)started (contract §2 Boot); a Plan like `smooth_ops`.

    The engine boots muted with Main at `-g-40` and Aux at 0 dB. Batch 1: Aux levels + Aux
    unmutes, Main parked at `start_db` (or the master, if quieter) while still muted. Then Main
    unmuted (the engine fades in over its ramp), then Main stepped linearly in dB to the saved
    master over `ramp_s`, one step per engine volume ramp so the rise is continuous. A muted
    state parks Main at the master and stays muted (a later unmute fades in as usual).
    Pass FADER_MAIN in `skip_faders` to adoption / `fader_drift` until the plan has run. A plan
    cut short leaves Main low, never loud: re-run it from the start. Never run it while the
    engine is already playing at the master: it would first drop Main to `start_db`.
    """
    s = state.clamped()
    lv = fader_levels(s)
    master = lv[FADER_MAIN]
    start = min(RANGES["master_db"].clamp(start_db, BOOT_GAIN_DB), master)
    aux: list[Op] = [Fader(FADER_SUBS, lv[FADER_SUBS]), Fader(FADER_TOPS, lv[FADER_TOPS]),
                     FaderMute(FADER_SUBS, False), FaderMute(FADER_TOPS, False)]
    if s.muted:
        return [(0.0, aux + [Fader(FADER_MAIN, master), FaderMute(FADER_MAIN, True)])]
    plan: Plan = [(0.0, aux + [Fader(FADER_MAIN, start)]),
                  (GUARD_WAIT_S, [FaderMute(FADER_MAIN, False)])]
    if master > start:
        step_s = VOLUME_RAMP_MS / 1000.0
        try:
            r = float(ramp_s)
        except (TypeError, ValueError, OverflowError):
            r = BOOT_RAMP_S
        r = min(max(r if math.isfinite(r) else BOOT_RAMP_S, 0.0), 60.0)
        n = max(1, math.ceil(r / step_s - 1e-9))
        for k in range(1, n + 1):
            v = master if k == n else round(start + (master - start) * k / n, 2)
            plan.append((step_s, [Fader(FADER_MAIN, v)]))
    return plan


# --------------------------------------------------------------------------- generate
def _gain(db: float, inverted: bool = False, description: str | None = None) -> dict[str, Any]:
    f: dict[str, Any] = {"type": "Gain",
                         "parameters": {"gain": db, "inverted": inverted, "mute": False, "scale": "dB"}}
    if description:
        f["description"] = description
    return f


def _biquad(kind: str, description: str | None = None, **params: float) -> dict[str, Any]:
    f: dict[str, Any] = {"type": "Biquad", "parameters": {"type": kind, **params}}
    if description:
        f["description"] = description
    return f


def _delay(ms: float) -> dict[str, Any]:
    return {"type": "Delay", "parameters": {"delay": ms, "delay_unit": "ms", "subsample": False}}


def _volume(fader: str, description: str) -> dict[str, Any]:
    return {"type": "Volume", "description": description,
            "parameters": {"fader": fader, "ramp_time_ms": VOLUME_RAMP_MS, "limit": AUX_LIMIT_DB}}


def _loudness(reference_db: float, low_boost_db: float) -> dict[str, Any]:
    """The subs' Loudness filter (state.LOUD_*): on the Main fader, low shelf only, no mid cut.
    The boost is low_boost * clamp((reference - Main) / 20, 0, 1), so it is at most 0.6 dB per dB
    of master reduction (low_boost <= 12): the subs are never louder than at the reference."""
    return {"type": "Loudness",
            "description": "sub loudness: low shelf grows as Main goes under the reference",
            "parameters": {"fader": "Main", "reference_level": reference_db,
                           "low_boost": low_boost_db, "high_boost": LOUD_HIGH_BOOST_DB,
                           "low_freq": LOUD_LOW_FREQ_HZ, "attenuate_mid": False}}


def _deharsh_compressor(threshold_db: float) -> dict[str, Any]:
    """`dh_comp`: monitors and compresses only the band copies 4/5 (state.DEHARSH_*). Its level is
    20*log10|band L + band R| smoothed in dB, so a centred source reads +6 dB over one channel.
    No makeup, no clipper: under the threshold its gain is exactly 0 dB."""
    return {"type": "Compressor", "description": "de-harsh: compresses the band copies only",
            "parameters": {"channels": DH_CHANNELS, "monitor_channels": list(DH_COMP_CHANNELS),
                           "process_channels": list(DH_COMP_CHANNELS),
                           "attack": DEHARSH_ATTACK_MS, "attack_unit": "ms",
                           "release": DEHARSH_RELEASE_MS, "release_unit": "ms",
                           "threshold": threshold_db, "factor": DEHARSH_RATIO, "makeup_gain": 0.0}}


def _src(channel: int, gain: float = 0.0, inverted: bool = False) -> dict[str, Any]:
    return {"channel": channel, "gain": gain, "inverted": inverted, "scale": "dB"}


def _alsa(device: str, channels: int, fmt: str | None) -> dict[str, Any]:
    d: dict[str, Any] = {"type": "Alsa", "channels": channels, "device": device}
    if fmt:
        d["format"] = fmt   # None = CamillaDSP picks the best format the device offers
    return d


def _input_mixer(profile: Profile, aux: tuple[int, ...] = ()) -> dict[str, Any]:
    """Program L/R from `input_map`. Every other capture channel joins L at linear gain 0
    (adds exactly nothing): CamillaDSP leaves a channel no mixer source uses empty, and
    5.0.0-beta.1's spectrum ring buffer then indexes that empty channel and panics the capture
    thread (the M4's mic inputs 1-2, 2026-10-16: the engine restarted every 2 s). Only the FIRST
    mixer counts for that, so it is this one that must name every capture channel.

    `aux` (`aux_inputs`): those capture channels become outputs 2[, 3] (A1, A2) at 0 dB instead
    of being padded; the others still pad. Without `aux` the mixer is exactly f2671b0's."""
    l, r = profile.input_map
    pad = [{"channel": c, "gain": 0.0, "inverted": False, "scale": "linear"}
           for c in range(profile.capture_channels) if c not in (l, r) and c not in aux]
    mapping = [{"dest": 0, "sources": [_src(l)] + pad}, {"dest": 1, "sources": [_src(r)]}]
    if not aux:
        return {
            "description": f"program L/R from capture channels {l}, {r}",
            "labels": ["L", "R"],
            "channels": {"in": profile.capture_channels, "out": 2},
            "mapping": mapping,
        }
    mapping += [{"dest": 2 + i, "sources": [_src(c)]} for i, c in enumerate(aux)]
    return {
        "description": f"program L/R from capture channels {l}, {r}; AUX from "
                       + ", ".join(str(c) for c in aux),
        "labels": ["L", "R"] + [f"AUX IN{c + 1}" for c in aux],
        "channels": {"in": profile.capture_channels, "out": 2 + len(aux)},
        "mapping": mapping,
    }


def _aux_mixer(aux_mode: str, n_aux: int) -> dict[str, Any]:
    """AUX into program L/R (`aux_mix`, 2 + n_aux -> 2). Line with A1 and A2: A1 -> L, A2 -> R at
    0 dB. Mic, or a line with one channel: A1 centred, AUX_MIC_PAN_DB to each side."""
    if aux_mode == "line" and n_aux >= 2:
        l_src, r_src, desc = [_src(2)], [_src(3)], "AUX line: A1 -> L, A2 -> R"
    else:
        l_src, r_src = [_src(2, AUX_MIC_PAN_DB)], [_src(2, AUX_MIC_PAN_DB)]
        desc = (f"AUX {'mic' if aux_mode == 'mic' else 'mono line'}: A1 centred "
                f"({AUX_MIC_PAN_DB:g} dB each side)")
    return {
        "description": desc,
        "labels": ["L", "R"],
        "channels": {"in": 2 + n_aux, "out": 2},
        "mapping": [{"dest": 0, "sources": [_src(0)] + l_src},
                    {"dest": 1, "sources": [_src(1)] + r_src}],
    }


def _dh_split_mixer() -> dict[str, Any]:
    """`dh_split`, 2 -> 6: L, R, then a copy of each to subtract (2, 3) and to compress (4, 5)."""
    return {
        "description": "de-harsh 2 -> 6: L, R, band copies to subtract (2, 3) and compress (4, 5)",
        "labels": ["L", "R", "L band -", "R band -", "L band comp", "R band comp"],
        "channels": {"in": 2, "out": DH_CHANNELS},
        "mapping": [{"dest": c, "sources": [_src(c % 2)]} for c in range(DH_CHANNELS)],
    }


def _dh_sum_mixer() -> dict[str, Any]:
    """`dh_sum`, 6 -> 2: x - band + compressed band, per side (exactly x while not compressing)."""
    return {
        "description": "de-harsh 6 -> 2: L - band + compressed band (exactly L under threshold)",
        "labels": ["L", "R"],
        "channels": {"in": DH_CHANNELS, "out": 2},
        "mapping": [{"dest": d, "sources": [_src(d), _src(2 + d, inverted=True), _src(4 + d)]}
                    for d in (0, 1)],
    }


def _split_mixer(mono_subs: bool) -> dict[str, Any]:
    if mono_subs:
        sub = [_src(0, -6.0), _src(1, -6.0)]
        subs = [{"dest": 0, "sources": sub}, {"dest": 1, "sources": copy.deepcopy(sub)}]
    else:
        subs = [{"dest": 0, "sources": [_src(0)]}, {"dest": 1, "sources": [_src(1)]}]
    return {
        "description": "subs " + ("mono (L+R -6 dB each)" if mono_subs else "stereo") + ", tops stereo",
        "labels": list(LOGICAL_NAMES),
        "channels": {"in": 2, "out": 4},
        "mapping": subs + [{"dest": 2, "sources": [_src(0)]}, {"dest": 3, "sources": [_src(1)]}],
    }


def _output_mixer(profile: Profile, audition: str) -> dict[str, Any]:
    n = profile.playback_channels
    mapping: list[dict[str, Any]] = []
    if profile.audition:
        per_side = {
            "sum": [[(0, -6.0), (2, -6.0)], [(1, -6.0), (3, -6.0)]],
            "tops": [[(2, 0.0)], [(3, 0.0)]],
            "subs": [[(0, 0.0)], [(1, 0.0)]],
        }.get(audition, [[(0, -6.0), (2, -6.0)], [(1, -6.0), (3, -6.0)]])
        for dest, srcs in enumerate(per_side[:n]):
            mapping.append({"dest": dest, "sources": [_src(c, g) for c, g in srcs]})
        labels = ["L", "R"][:n] + [f"out{i + 1}" for i in range(2, n)]
        desc = f"dev audition: {audition}"
    else:
        for phys, logical in enumerate(profile.output_map[:n]):
            mapping.append({"dest": phys, "sources": [_src(logical)]})
        labels = [f"out{i + 1} " + (LOGICAL_NAMES[profile.output_map[i]] if i < len(profile.output_map) else "-")
                  for i in range(n)]
        desc = "logical -> physical outputs"
    return {"description": desc, "labels": labels, "channels": {"in": 4, "out": n}, "mapping": mapping}


def generate(profile: Profile, state: State) -> dict[str, Any]:
    """The full CamillaDSP v5 config (a dict ready for `to_yaml` / `json.dumps`).

    With aux_mode "off", loud_on and deharsh_on False it is byte for byte f2671b0's config (key
    order included: the optional filters, mixers and steps are inserted, never reordered).
    """
    s = state.clamped()
    pm = param_map(s)
    aux = aux_inputs(profile, s.aux_mode)

    def v(name: str, key: str) -> Any:
        return pm[_pp(name, key)]

    filters: dict[str, Any] = {"in_trim": _gain(v("in_trim", "gain"), description="input trim")}
    if aux_on(s):          # from the state alone (module docstring); routed only if `aux`
        filters[AUX_GAIN] = _gain(v(AUX_GAIN, "gain"), description="AUX level (not the input trim)")
        if s.aux_mode == "mic":
            filters[AUX_HP] = _biquad("Highpass", f"AUX mic high-pass {AUX_MIC_HP_HZ:g} Hz, 12 dB/oct",
                                      freq=AUX_MIC_HP_HZ, q=AUX_MIC_HP_Q)
    for name in SYS_PEQ + LIVE_PEQ:
        filters[name] = _biquad("Peaking", freq=v(name, "freq"), gain=v(name, "gain"), q=v(name, "q"))
    filters["live_lowshelf"] = _biquad("Lowshelf", freq=v("live_lowshelf", "freq"),
                                       gain=v("live_lowshelf", "gain"), q=v("live_lowshelf", "q"))
    filters["live_highshelf"] = _biquad("Highshelf", freq=v("live_highshelf", "freq"),
                                        gain=v("live_highshelf", "gain"), q=v("live_highshelf", "q"))
    if s.deharsh_on:
        filters[DH_BAND] = _biquad("Bandpass", f"de-harsh band (Q {DEHARSH_Q:g})",
                                   freq=v(DH_BAND, "freq"), q=DEHARSH_Q)
    filters["sub_trim"] = _gain(0.0, v("sub_trim", "inverted"), "subs polarity")
    filters["sub_lowpass"] = _biquad("Lowpass", f"sub low-pass (off = {SUB_LOWPASS_OFF_HZ:g} Hz)",
                                     freq=v("sub_lowpass", "freq"), q=SUB_LOWPASS_Q)
    if s.loud_on:
        filters[SUB_LOUDNESS] = _loudness(v(SUB_LOUDNESS, "reference_level"),
                                          v(SUB_LOUDNESS, "low_boost"))
    filters["sub_delay"] = _delay(v("sub_delay", "delay"))
    filters["sub_fader"] = _volume("Aux1", "subs group (balance +b/2)")
    filters["top_trim"] = _gain(0.0, v("top_trim", "inverted"), "tops polarity")
    filters["top_lowcut"] = _biquad("Highpass", f"tops Lo-Cut 12 dB/oct (off = {LOWCUT_OFF_HZ:g} Hz)",
                                    freq=v("top_lowcut", "freq"), q=LOWCUT_Q)
    filters["top_delay"] = _delay(v("top_delay", "delay"))
    filters["top_fader"] = _volume("Aux2", "tops group (balance -b/2)")
    for i, name in enumerate(OUT_TRIM):
        filters[name] = _gain(v(name, "gain"), description=f"output trim {LOGICAL_NAMES[i]}")

    pipeline: list[dict[str, Any]] = []
    if aux:
        mic = s.aux_mode == "mic"
        pipeline += [
            {"type": "Mixer", "name": "input", "description": "program L/R + AUX"},
            {"type": "Filter", "channels": [0, 1], "names": ["in_trim"],
             "description": "program input trim (not the AUX)"},
            {"type": "Filter", "channels": [2] if mic or len(aux) == 1 else [2, 3],
             "names": [AUX_GAIN, AUX_HP] if mic else [AUX_GAIN],
             "description": "AUX level" + (" + mic high-pass" if mic else "")},
            {"type": "Mixer", "name": AUX_MIX, "description": "AUX into program L/R"},
            {"type": "Filter", "channels": [0, 1], "names": list(PROGRAM_EQ),
             "description": "system PEQ, live EQ (program + AUX)"},
        ]
    else:
        pipeline += [
            {"type": "Mixer", "name": "input", "description": "program L/R"},
            {"type": "Filter", "channels": [0, 1], "names": list(INPUT_CHAIN),
             "description": "trim, system PEQ, live EQ"},
        ]
    if s.deharsh_on:
        pipeline += [
            {"type": "Mixer", "name": DH_SPLIT, "description": "de-harsh: L/R + band copies"},
            {"type": "Filter", "channels": list(DH_BAND_CHANNELS), "names": [DH_BAND],
             "description": "de-harsh band"},
            {"type": "Processor", "name": DH_COMP, "description": "de-harsh: compress the band"},
            {"type": "Mixer", "name": DH_SUM, "description": "de-harsh: x - band + compressed band"},
        ]
    sub_chain = list(SUB_CHAIN)
    if s.loud_on:
        sub_chain.insert(sub_chain.index("sub_lowpass") + 1, SUB_LOUDNESS)
    pipeline += [
        {"type": "Mixer", "name": "split", "description": "2 -> 4: Sub L/R, Top L/R"},
        {"type": "Filter", "channels": [0, 1], "names": sub_chain, "description": "subs"},
        {"type": "Filter", "channels": [2, 3], "names": list(TOP_CHAIN), "description": "tops"},
    ]
    for i, name in enumerate(OUT_TRIM):
        pipeline.append({"type": "Filter", "channels": [i], "names": [name],
                         "description": f"trim {LOGICAL_NAMES[i]}"})
    pipeline.append({"type": "Mixer", "name": "output", "description": "to physical outputs"})

    mixers: dict[str, Any] = {"input": _input_mixer(profile, aux)}
    if aux:
        mixers[AUX_MIX] = _aux_mixer(s.aux_mode, len(aux))
    if s.deharsh_on:
        mixers[DH_SPLIT] = _dh_split_mixer()
        mixers[DH_SUM] = _dh_sum_mixer()
    mixers["split"] = _split_mixer(s.mono_subs)
    mixers["output"] = _output_mixer(profile, s.dev_audition)
    cfg: dict[str, Any] = {
        "title": f"K1000 {profile.name}",
        "description": f"Generated by k1000 (profile {profile.name}). Owned parameters edited "
                       "in CamillaGUI are adopted; structure changes are overwritten.",
        "devices": {
            "samplerate": SAMPLERATE,
            "chunksize": CHUNKSIZE,
            "target_level": int(getattr(profile, "target_level", TARGET_LEVEL) or TARGET_LEVEL),
            "enable_rate_adjust": False,
            "volume_ramp_time_ms": VOLUME_RAMP_MS,
            "volume_limit": MAIN_LIMIT_DB,
            "capture": _alsa(profile.capture_device, profile.capture_channels, profile.capture_format),
            "playback": _alsa(profile.playback_device, profile.playback_channels, profile.playback_format),
        },
        "mixers": mixers,
        "filters": filters,
    }
    if s.deharsh_on:
        cfg["processors"] = {DH_COMP: _deharsh_compressor(pm[_proc_pp(DH_COMP, "threshold")])}
    cfg["pipeline"] = pipeline
    return cfg


# libyaml's emitter when PyYAML has it (python3-yaml on the box does): the same text as the
# pure-Python SafeDumper (same representer; tested byte for byte), ~4x faster. The pure-Python
# dump took 34 ms on the Pi, in the event loop, every 0.25 s while MASTER went down: the main
# CPU-bound holder of the GIL the knob worker waits for (PERF-3).
YAML_DUMPER: Any = getattr(yaml, "CSafeDumper", None) if getattr(yaml, "__with_libyaml__", False) \
    else None
YAML_DUMPER = YAML_DUMPER or yaml.SafeDumper


def to_yaml(cfg: dict[str, Any], dumper: Any = None) -> str:
    """YAML text for active.yml (key order kept, so the file reads like the pipeline)."""
    head = ("# Generated by k1000.dspconfig - do not hand-edit (rewritten on every change).\n"
            f"# {cfg.get('title', '')}\n")
    return head + yaml.dump(cfg, Dumper=dumper or YAML_DUMPER, sort_keys=False,
                            default_flow_style=False, allow_unicode=True, width=100)


def signal_generator_variant(cfg: dict[str, Any], level_db: float = -30.0, freq: float = 1000.0,
                             filename: str = "/dev/null") -> dict[str, Any]:
    """Same config on hardware-free devices (SignalGenerator -> File), for engine tests.

    Channel counts are kept so the input/output mixers stay valid.
    """
    out = copy.deepcopy(cfg)
    dev = out["devices"]
    dev["capture"] = {"type": "SignalGenerator", "channels": dev["capture"]["channels"],
                      "signal": {"type": "Sine", "freq": freq, "level": level_db}}
    dev["playback"] = {"type": "File", "channels": dev["playback"]["channels"],
                       "filename": filename, "format": "S32_LE"}
    return out


# --------------------------------------------------------------------------- diff
def diff_ops(old_state: State, new_state: State) -> list[Op]:
    """Minimal ops taking the engine from `old_state` to `new_state` (both clamped first).

    A mixer/device change yields one `Rebuild(reason)` and no SetValue ops (the regenerated
    config already carries every value); fader ops are always listed because faders live
    outside the config. `dev_audition` only rebuilds while `new_state.profile == "dev"` (the
    controller keeps `state.profile` equal to the running profile). Switching AUX IN, sub
    loudness or de-harsh adds/removes their blocks: a Rebuild whatever the profile (on dev an
    AUX change only adds/removes its unrouted filters, still a config change).
    """
    o, n = old_state.clamped(), new_state.clamped()
    ops: list[Op] = []
    reasons: list[str] = []
    if o.profile != n.profile:
        reasons.append(f"profile {o.profile} -> {n.profile}")
    if o.mono_subs != n.mono_subs:
        reasons.append(f"mono_subs {o.mono_subs} -> {n.mono_subs}")
    if o.dev_audition != n.dev_audition and n.profile == "dev":
        reasons.append(f"dev_audition {o.dev_audition} -> {n.dev_audition}")
    for name in ("aux_mode", "loud_on", "deharsh_on"):
        if getattr(o, name) != getattr(n, name):
            reasons.append(f"{name} {getattr(o, name)} -> {getattr(n, name)}")
    if reasons:
        ops.append(Rebuild("; ".join(reasons)))
    else:
        po, pn = param_map(o), param_map(n)
        ops += [SetValue(k, val) for k, val in pn.items() if po.get(k) != val]
    fo, fn = fader_levels(o), fader_levels(n)
    for i in (FADER_SUBS, FADER_TOPS, FADER_MAIN):
        if fo[i] != fn[i]:
            ops.append(Fader(i, fn[i]))
    if o.muted != n.muted:
        ops.append(FaderMute(FADER_MAIN, n.muted))
    return ops


# --------------------------------------------------------------------------- click-safe plans
# Measured on the box (CamillaDSP 5.0.0-beta.1, 200 Hz sine, real-time paced capture):
# one detent (0.5 dB, 1/12 oct, one Q step) of a Biquad or Gain is clean; big jumps click
# (Lo-Cut 100 -> 10 Hz, a -12 -> 0 dB reset, a 6 dB trim step); every Delay change and every
# polarity flip clicks (the delay line is rebuilt; the sign flips at once). So big moves are
# split into detent-sized steps, and delay/polarity changes are done under a muted group fader.
STEP_WAIT_S = 0.012          # > 2 chunks of 256 @ 48 kHz: each step reaches the audio before the next
GUARD_WAIT_S = VOLUME_RAMP_MS / 1000.0 + 0.05   # Aux fader mute ramp + margin

Plan = list[tuple[float, list[Op]]]


def _guard_group(ptr: str) -> int | None:
    name = _unpointer(ptr)[1] if ptr.startswith("/filters/") else ""
    if name in ("sub_delay", "sub_trim"):
        return FADER_SUBS
    if name in ("top_delay", "top_trim"):
        return FADER_TOPS
    return None


# Owned values that raise the PA the way a trim does, applied by the engine as an instant step:
# the Gain filters (input/output trims, the AUX level) and the subs' loudness boost (a gain the
# Loudness filter writes straight into its shelf). They move in `max_trim_step` steps.
_TRIM_POINTERS = frozenset({_pp("in_trim", "gain"), _pp(AUX_GAIN, "gain"),
                            _pp(SUB_LOUDNESS, "low_boost"), *(_pp(n, "gain") for n in OUT_TRIM)})


def _is_trim(ptr: str) -> bool:
    return ptr in _TRIM_POINTERS


def smooth_ops(old_state: State, new_state: State, *, max_db_step: float = 0.5,
               max_trim_step: float = 0.1, max_oct_step: float = 1.0 / 12.0,
               max_steps: int = 100) -> Plan:
    """`diff_ops`, re-planned so no single engine write is big enough to click.

    Returns [(wait_before_s, ops), ...]: sleep, then send that batch (one PatchConfig when it
    holds several SetValue). Biquad gains/frequencies/Q move in steps of at most `max_db_step`
    dB / `max_oct_step` octave (the engine smooths each Biquad switch); Gain-filter trims, which
    the engine applies as an instant step, use `max_trim_step`, and so do the AUX level and the
    sub loudness boost (`_is_trim`). The loudness reference and the de-harsh threshold (dB) move
    like a Biquad gain, the de-harsh frequency like any frequency. Steps are `STEP_WAIT_S` apart,
    at most `max_steps` of them. Delay and polarity changes are wrapped in a mute
    of their group fader (Aux1 subs / Aux2 tops): mute, wait the ramp, write, unmute. Fader ops
    go first (the engine ramps them). A Rebuild is returned as-is (a setup action: mute Main
    around it). Typical use: SETUP › Lo-Cut on/off, SETUP › Reset live EQ (and its undo), and
    coalesced knob moves.

    A guarded plan cut short between its mute and unmute (task cancel, websocket drop, k1000
    crash) leaves a group muted with the engine still running. So: send
    `plan_release_ops(plan)` in a `finally:` (and again after a reconnect), pass the guarded
    group in `skip_faders` while the plan runs, and run `fader_drift` after it — that releases
    any Aux mute K1000 did not just set, including one left by a crash before a restart.
    """
    ops = diff_ops(old_state, new_state)
    if any(isinstance(o, Rebuild) for o in ops):
        return [(0.0, ops)]
    faders = [o for o in ops if not isinstance(o, SetValue)]
    sets = [o for o in ops if isinstance(o, SetValue)]
    guarded = [o for o in sets if _guard_group(o.pointer) is not None]
    ramped = [o for o in sets if _guard_group(o.pointer) is None]
    plan: Plan = [(0.0, faders)] if faders else []

    old = param_map(old_state)
    tracks: list[tuple[str, float, float, bool]] = []   # pointer, from, to, log scale
    n_steps = 1
    for o in ramped:
        a, b = old.get(o.pointer), o.value
        if _num(a) is None or _num(b) is None:
            tracks.append((o.pointer, b, b, False))
            continue
        is_log = o.pointer.endswith(("/freq", "/q")) and a > 0 and b > 0
        if is_log:
            span = abs(math.log2(b / a)) / max_oct_step
        else:
            span = abs(b - a) / (max_trim_step if _is_trim(o.pointer) else max_db_step)
        n_steps = min(max_steps, max(n_steps, math.ceil(span - 1e-9)))
        tracks.append((o.pointer, float(a), float(b), is_log))
    if tracks:
        for k in range(1, n_steps + 1):
            t = k / n_steps
            batch: list[Op] = []
            for ptr, a, b, is_log in tracks:
                if k == n_steps:
                    v = b
                elif is_log:
                    v = round(a * (b / a) ** t, 3)
                else:
                    v = round(a + (b - a) * t, 3)
                batch.append(SetValue(ptr, v))
            plan.append((STEP_WAIT_S if plan else 0.0, batch))

    groups = sorted({_guard_group(o.pointer) for o in guarded})  # type: ignore[type-var]
    if groups:
        plan.append((STEP_WAIT_S if plan else 0.0, [FaderMute(g, True) for g in groups if g is not None]))
        plan.append((GUARD_WAIT_S, list(guarded)))
        plan.append((STEP_WAIT_S, [FaderMute(g, False) for g in groups if g is not None]))
    return plan


def plan_release_ops(plan: Plan) -> list[Op]:
    """The group unmutes a plan ends with: send them in a `finally:` so a guard never sticks."""
    return [op for _w, batch in plan for op in batch
            if isinstance(op, FaderMute) and op.index in (FADER_SUBS, FADER_TOPS) and not op.mute]


# --------------------------------------------------------------------------- adoption
def _as_cfg(cfg_json: Any) -> dict[str, Any]:
    if isinstance(cfg_json, (bytes, bytearray)):
        cfg_json = cfg_json.decode("utf-8", "replace")
    if isinstance(cfg_json, str):
        try:
            cfg_json = json.loads(cfg_json)
        except ValueError:
            return {}
    return cfg_json if isinstance(cfg_json, dict) else {}


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _params(cfg: dict[str, Any], name: str) -> dict[str, Any]:
    return _dict(_dict(_dict(cfg.get("filters")).get(name)).get("parameters"))


def _proc_params(cfg: dict[str, Any], name: str) -> dict[str, Any]:
    return _dict(_dict(_dict(cfg.get("processors")).get(name)).get("parameters"))


def _gain_db(p: dict[str, Any]) -> float | None:
    g = _num(p.get("gain"))
    if g is None:
        return None
    if str(p.get("scale") or "dB").lower() == "linear":
        return 20.0 * math.log10(abs(g)) if g else -150.0
    return g


def _q_of(p: dict[str, Any]) -> float | None:
    q = _num(p.get("q"))
    if q is not None:
        return q
    bw = _num(p.get("bandwidth"))     # octaves -> Q (same relation CamillaDSP uses)
    if bw is not None and bw > 0:
        x = 2.0 ** bw
        return math.sqrt(x) / (x - 1.0)
    return None


def _delay_ms(p: dict[str, Any], samplerate: float) -> float | None:
    d = _num(p.get("delay"))
    if d is None:
        return None
    unit = str(p.get("delay_unit") or "ms")
    factor = {"s": 1000.0, "ms": 1.0, "us": 0.001, "mm": 1.0 / 343.0,
              "samples": 1000.0 / samplerate if samplerate else None}.get(unit)
    return d * factor if factor else None


def _fader_at(faders: Any, i: int) -> tuple[float | None, bool | None]:
    try:
        f = faders[i]
    except (TypeError, IndexError, KeyError):
        return None, None
    if isinstance(f, dict):
        vol, mute = f.get("volume"), f.get("mute")
    elif isinstance(f, (list, tuple)) and len(f) >= 2:
        vol, mute = f[0], f[1]
    else:
        return None, None
    return _num(vol), mute if isinstance(mute, bool) else None


def _sources(mixer: Any, dest: int) -> list[tuple[int, float]]:
    if not isinstance(mixer, dict):
        return []
    for m in mixer.get("mapping") or []:
        if isinstance(m, dict) and m.get("dest") == dest:
            return sorted((s["channel"], _num(s.get("gain")) or 0.0)
                          for s in (m.get("sources") or [])
                          if isinstance(s, dict) and isinstance(s.get("channel"), int))
    return []


def feature_switches(cfg_json: Any) -> dict[str, Any]:
    """The D-016 switches a K1000-generated config was made with: {"aux_mode": "off" | "mic" |
    "line", "loud_on": bool, "deharsh_on": bool}, read from its block DEFINITIONS (they depend on
    the State alone, module docstring: an AUX on dev is defined, unrouted). {} when `cfg_json` has
    no pipeline (not a config).

    For `control.recover_state` only: active.yml is K1000's own file, so its blocks are the
    switches it last ran with, as its split mixer is the mono setting. A RUNNING config is never
    read this way (`state_from_config`): there a missing or extra block is a structure drift."""
    cfg = _as_cfg(cfg_json)
    if not isinstance(cfg.get("pipeline"), list):
        return {}
    filters, procs = _dict(cfg.get("filters")), _dict(cfg.get("processors"))
    aux = ("mic" if AUX_HP in filters else "line") if AUX_GAIN in filters else "off"
    return {"aux_mode": aux, "loud_on": SUB_LOUDNESS in filters, "deharsh_on": DH_COMP in procs}


def state_from_config(cfg_json: Any, faders: Any, base_state: State, *,
                      skip_pointers: Iterable[str] = (), skip_faders: Iterable[int] = ()) -> State:
    """`base_state` updated with the owned values found in the running config and faders.

    `cfg_json` = GetConfigJson value (JSON string or dict); `faders` = GetFaders value
    (list of {"volume", "mute"}, or None to skip). Anything missing or malformed keeps the base
    value; the result is clamped, so an out-of-range GUI edit is adopted at the nearest legal
    value. Then run `config_drift(live, generate(profile, adopted))` (pushes owned config
    values back, e.g. the clamped one) and `fader_drift(faders, adopted)` (pushes faders back;
    `config_drift` does not look at faders).

    Faders: Main volume + mute are adopted (clamped: +12 dB becomes 0 dB, and `fader_drift`
    then pulls the engine down). Aux1/Aux2 are adopted as `balance_db = Aux1 - Aux2` only when
    they form a balance pair (Aux1 = -Aux2 within BALANCE_TOL_DB); a common-mode or one-sided
    GUI move is not adopted and `fader_drift` reverts it. Aux mutes are never adopted (K1000
    owns them: the delay/polarity guard). A delay is adopted only when it changes the whole
    sample count (the engine resolution), so the state keeps its own ms value otherwise.

    The optional blocks' owned values (`aux_gain` gain, `sub_loudness` reference_level and
    low_boost, `dh_band` freq, `dh_comp` threshold) are adopted wherever the running config has
    them. Their switches (aux_mode, loud_on, deharsh_on) are never read from the structure: a
    config without a block the state has on is a structure drift (`config_drift`) that the
    upload restores, and blocks the state has off do not switch it on. (K1000's own active.yml
    is the exception, read by `feature_switches` when state.json is torn.)

    `skip_pointers` / `skip_faders`: values K1000 wrote itself in the last ~0.2 s. Measured on
    the box: GetConfigJson issued right after a SetConfigValue returns the previous value ~2 %
    of the time (for < 2 ms), and adopting that would undo the knob move. Also skip the Main
    fader while a boot ramp runs.
    """
    cfg = _as_cfg(cfg_json)
    skip = frozenset(skip_pointers)
    if skip:
        cfg = copy.deepcopy(cfg)
        for ptr in skip:
            parts = _unpointer(ptr)
            node: Any = cfg
            for p in parts[:-1]:
                node = node.get(p) if isinstance(node, dict) else None
            if isinstance(node, dict):
                node.pop(parts[-1], None)
    skip_f = frozenset(skip_faders)
    s = base_state.copy()
    sr = _num(_dict(cfg.get("devices")).get("samplerate")) or SAMPLERATE

    g = _gain_db(_params(cfg, "in_trim"))
    if g is not None:
        s.input_trim_db = g
    targets: list[tuple[str, PeqBand | ShelfBand]] = [
        *zip(SYS_PEQ, s.sys_peq), *zip(LIVE_PEQ, s.live_peq),
        ("live_lowshelf", s.live_lowshelf), ("live_highshelf", s.live_highshelf),
    ]
    for name, band in targets:
        p = _params(cfg, name)
        f, gn, q = _num(p.get("freq")), _num(p.get("gain")), _q_of(p)
        if f is not None:
            band.freq = f
        if gn is not None:
            if band.on is False:
                # OUT (D-014): the engine plays 0 dB and the band keeps its own gain. Anything
                # else there was set outside K1000 (CamillaGUI): that is what plays, so it is
                # adopted as the band's gain, and the band IN.
                if abs(gn) > OUT_GAIN_TOL_DB:
                    band.gain, band.on = gn, True
            else:
                band.gain = gn
        if q is not None:
            band.q = q
    for name, attr in (("sub_trim", "sub_inverted"), ("top_trim", "top_inverted")):
        inv = _params(cfg, name).get("inverted")
        if isinstance(inv, bool):
            setattr(s, attr, inv)
    f = _num(_params(cfg, "sub_lowpass").get("freq"))
    if f is not None:
        if f >= SUB_LOWPASS_OFF_HZ - 1.0:
            s.sub_lowpass_on = False
        else:
            s.sub_lowpass_on, s.sub_lowpass_hz = True, f
    f = _num(_params(cfg, "top_lowcut").get("freq"))
    if f is not None:
        if f <= LOWCUT_OFF_HZ + 0.5:
            s.lowcut_on = False
        else:
            s.lowcut_on, s.lowcut_hz = True, f
    for name, attr in (("sub_delay", "sub_delay_ms"), ("top_delay", "top_delay_ms")):
        d = _delay_ms(_params(cfg, name), sr)
        if d is not None and delay_samples(d) != delay_samples(getattr(s, attr)):
            setattr(s, attr, d)
    for i, name in enumerate(OUT_TRIM):
        g = _gain_db(_params(cfg, name))
        if g is not None:
            s.out_trim_db[i] = g
    g = _gain_db(_params(cfg, AUX_GAIN))
    if g is not None:
        s.aux_gain_db = g
    loud = _params(cfg, SUB_LOUDNESS)
    for key, attr in (("reference_level", "loud_ref_db"), ("low_boost", "loud_boost_db")):
        x = _num(loud.get(key))
        if x is not None:
            setattr(s, attr, x)
    f = _num(_params(cfg, DH_BAND).get("freq"))
    if f is not None:
        s.deharsh_hz = f
    x = _num(_proc_params(cfg, DH_COMP).get("threshold"))
    if x is not None:
        s.deharsh_thresh_db = x

    mixers = _dict(cfg.get("mixers"))
    sub_l = _sources(mixers.get("split"), 0)
    if len(sub_l) == 2:
        s.mono_subs = True
    elif len(sub_l) == 1:
        s.mono_subs = False
    out = _dict(mixers.get("output"))
    if s.profile == "dev" and _dict(out.get("channels")).get("out") == 2:
        pair = (tuple(c for c, _g in _sources(out, 0)), tuple(c for c, _g in _sources(out, 1)))
        aud = {((0, 2), (1, 3)): "sum", ((2,), (3,)): "tops", ((0,), (1,)): "subs"}.get(pair)
        if aud:
            s.dev_audition = aud

    if faders is not None:
        if FADER_MAIN not in skip_f:
            vol, mute = _fader_at(faders, FADER_MAIN)
            if vol is not None:
                s.master_db = vol
            if mute is not None:
                s.muted = mute
        if FADER_SUBS not in skip_f and FADER_TOPS not in skip_f:
            v1, _ = _fader_at(faders, FADER_SUBS)
            v2, _ = _fader_at(faders, FADER_TOPS)
            if v1 is not None and v2 is not None and abs(v1 + v2) <= BALANCE_TOL_DB:
                s.balance_db = v1 - v2
    return s.clamped()


# --------------------------------------------------------------------------- fader drift
def fader_drift(faders: Any, state: State, *, skip_faders: Iterable[int] = (),
                tol_db: float = FADER_TOL_DB) -> list[Op]:
    """Fader ops that bring the engine's faders back to what `state` (and the screen) says.

    `faders` = GetFaders value. Call it after `state_from_config` (so a legal Main move from
    CamillaGUI is adopted first, not reverted) with the same `skip_faders`: K1000's own fader
    writes of the last ~0.2 s (read-after-write race), FADER_MAIN while `boot_ramp_plan` runs,
    the guarded group while a `smooth_ops` guard runs.

    - Aux1/Aux2 are K1000's (balance macro, delay/polarity guard): a volume that differs from
      `fader_levels(state)` is re-sent and any mute is released. This recovers a guard mute left
      by a plan cut short and a common-mode/one-sided GUI drag.
    - Main is only ever pulled DOWN or muted here: a hidden value above the state's master
      (e.g. +12 dB stored while volume_limit hid it), or a state mute the engine lost. Raising or
      unmuting Main is for the knob or `boot_ramp_plan`, so calling this right after an engine
      restart (Main muted at -40) can never jump the PA to show level.
    Main ops come first. Missing/malformed fader entries give no op.
    """
    s = state.clamped()
    lv = fader_levels(s)
    skip = frozenset(skip_faders)
    ops: list[Op] = []
    if FADER_MAIN not in skip:
        vol, mute = _fader_at(faders, FADER_MAIN)
        if vol is not None and vol > lv[FADER_MAIN] + tol_db:
            ops.append(Fader(FADER_MAIN, lv[FADER_MAIN]))
        if mute is False and s.muted:
            ops.append(FaderMute(FADER_MAIN, True))
    for i in (FADER_SUBS, FADER_TOPS):
        if i in skip:
            continue
        vol, mute = _fader_at(faders, i)
        if vol is not None and abs(vol - lv[i]) > tol_db:
            ops.append(Fader(i, lv[i]))
        if mute is True:
            ops.append(FaderMute(i, False))
    return ops


# --------------------------------------------------------------------------- drift check
def _close(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    na, nb = _num(a), _num(b)
    if na is not None and nb is not None:
        return math.isclose(na, nb, rel_tol=1e-6, abs_tol=1e-6)
    return a == b


def _norm_mixer(m: Any) -> Any:
    if not isinstance(m, dict):
        return None
    ch = m.get("channels") or {}
    maps = []
    for e in m.get("mapping") or []:
        if not isinstance(e, dict):
            return None
        srcs = sorted(
            (x["channel"] if isinstance(x.get("channel"), int) else -1,
             round(_num(x.get("gain")) or 0.0, 4), bool(x.get("inverted")), bool(x.get("mute")),
             str(x.get("scale") or "dB"))
            for x in (e.get("sources") or []) if isinstance(x, dict))
        if srcs or e.get("mute"):
            maps.append((int(e.get("dest", -1)), bool(e.get("mute")), tuple(srcs)))
    return (ch.get("in"), ch.get("out"), tuple(sorted(maps)))


def _norm_step(st: Any) -> Any:
    if not isinstance(st, dict):
        return None
    names = st.get("names")
    chans = st.get("channels")
    return (st.get("type"), st.get("name"), tuple(names) if isinstance(names, list) else None,
            tuple(chans) if isinstance(chans, list) else None, bool(st.get("bypassed")))


class _DriftFields(NamedTuple):
    devices: list[str]
    rebuild: list[str]
    fixes: list[SetValue]


class Drift(_DriftFields):
    """What differs between the running config and what K1000 would generate (`config_drift`).

    devices: the `devices` block differs: any scalar (volume_limit, volume_ramp_time_ms,
        target_level, enable_rate_adjust, samplerate, chunksize), a capture/playback key, or a
        device option K1000 leaves unset (resampler, silence_threshold...). Measured on
        5.0.0-beta.1: ANY devices change, even volume_limit alone via SetConfigValue/PatchConfig,
        logs "Devices changed, restart required" and stops/restarts capture and playback, i.e.
        an audio drop. Never fix it automatically while audio runs: show it, and upload
        `expected` from SETUP (with confirm) or while muted at boot. A hidden Main above range
        is handled meanwhile by `fader_drift`.
    rebuild: mixers / pipeline / a filter's or processor's presence, type, subtype or a missing
        key (e.g. Q replaced by bandwidth) differ: a whole-config upload rebuilds the pipeline
        (possible click). Without a device restart only if `devices` is empty; otherwise upload
        `with_live_devices(expected, live)` (measured: "Rebuilding pipeline" only, no device
        restart, devices drift still reported) and leave the device fix for SETUP.
    fixes: owned/fixed parameter values that differ while the structure matches: send them as
        ONE PatchConfig(ops_to_patch(fixes)) (no restart, no rebuild). Empty when `rebuild` is
        not (the upload carries them). A filter (or processor) whose unit/scale key differs gets
        ALL its parameters in `fixes`, so value and unit change together. Processor values are
        `/processors/<name>/parameters/<key>` pointers (de-harsh's `dh_comp`).
    held: cuts made outside K1000 on the AUX routing (`mixer_cuts`: an `aux_mix` source or
        destination muted or turned down, an AUX source of the `input` mixer turned down), the
        only difference of their mixer: kept and shown, never a rebuild. A rebuild would put
        the mic back up (`control._keep_cuts` carries them into any restore instead), and since
        a kept cut still differs, it would rebuild again every restore interval. An attribute,
        not a tuple field: a Drift still unpacks as (devices, rebuild, fixes).
    """

    held: tuple[str, ...]

    def __new__(cls, devices: list[str], rebuild: list[str], fixes: list[SetValue],
                held: Iterable[str] = ()) -> "Drift":
        self = super().__new__(cls, devices, rebuild, fixes)
        self.held = tuple(held)
        return self

    def __repr__(self) -> str:
        return f"{super().__repr__()[:-1]}, held={self.held!r})"

    @property
    def clean(self) -> bool:
        return not (self.devices or self.rebuild or self.fixes or self.held)


_DEVICE_SIDES = ("capture", "playback")
# change the meaning of the other values of the filter / processor
_UNIT_KEYS = ("delay_unit", "scale", "attack_unit", "release_unit")


def _device_drift(ld: dict[str, Any], ed: dict[str, Any], where: str, out: list[str]) -> None:
    for key in sorted(set(ld) | set(ed)):
        if key in _DEVICE_SIDES or key == "signal":     # sides handled by the caller; signal = test generator
            continue
        if key in ed:
            if not _close(ld.get(key), ed[key]):
                out.append(f"{where}{key} {ld.get(key)!r} != {ed[key]!r}")
        elif key == "format":
            continue        # K1000 leaves it unset = CamillaDSP auto-selects; whatever runs is fine
        elif ld.get(key) is not None:
            out.append(f"{where}{key} {ld[key]!r} set (K1000 leaves it unset)")


def with_live_devices(expected: dict[str, Any], live_cfg: Any) -> dict[str, Any]:
    """`expected` with the running `devices` block: a whole-config upload that only rebuilds the
    pipeline (mixers/filters) and does not restart the audio devices."""
    out = copy.deepcopy(expected)
    ld = _as_cfg(live_cfg).get("devices")
    if isinstance(ld, dict):
        out["devices"] = copy.deepcopy(ld)
    return out


def config_drift(live_cfg: Any, expected: dict[str, Any], *,
                 skip_pointers: Iterable[str] = ()) -> Drift:
    """Compare the running config (GetConfigJson) with what K1000 would generate.

    Returns a `Drift(devices, rebuild, fixes, held)`; see `Drift` for what each one costs and
    when the controller may act on it. Call it after `state_from_config` so owned edits are
    adopted, not reverted. Delays compare as whole samples (the engine's resolution), so a GUI
    value like 1.009 ms against K1000's 1.0 ms (both 48 samples) is not a difference: a write
    would rebuild the delay line and click for nothing. `skip_pointers`: values K1000 wrote in
    the last ~0.2 s (a stale read must not trigger a redundant write).

    Every mixer, filter and processor `expected` has is compared (the optional blocks
    included); the running config's extra ones are not, unless the pipeline uses them (then the
    pipeline differs). A `processors` section absent or null on either side is an empty one.
    """
    live = _as_cfg(live_cfg)
    skip = frozenset(skip_pointers)
    devices: list[str] = []
    rebuild: list[str] = []
    ld, ed = _dict(live.get("devices")), _dict(expected.get("devices"))
    _device_drift(ld, ed, "devices.", devices)
    for side in _DEVICE_SIDES:
        _device_drift(_dict(ld.get(side)), _dict(ed.get(side)), f"devices.{side}.", devices)
    lmix, emix = _dict(live.get("mixers")), _dict(expected.get("mixers"))
    held: list[str] = []
    for name in (*MIXERS, *(n for n in emix if n not in MIXERS)):
        if _norm_mixer(lmix.get(name)) != _norm_mixer(emix.get(name)):
            cuts = mixer_cuts(name, lmix.get(name), emix.get(name))
            if cuts is None:
                rebuild.append(f"mixer {name} differs")
            else:
                held += cuts
    lpipe = live.get("pipeline")
    lp = [_norm_step(st) for st in (lpipe if isinstance(lpipe, list) else [])]
    ep = [_norm_step(st) for st in expected.get("pipeline") or []]
    if lp != ep:
        rebuild.append("pipeline differs")

    live_sr = _num(ld.get("samplerate")) or SAMPLERATE
    fixes: list[SetValue] = []
    for section, kind, ptr in (("filters", "filter", _pp), ("processors", "processor", _proc_pp)):
        lsec = _dict(live.get(section))
        for name, ef in _dict(expected.get(section)).items():
            fixes += _block_drift(kind, name, lsec.get(name), ef, ptr, skip, live_sr, rebuild)
    return Drift(devices, rebuild, [] if rebuild else fixes, tuple(held))


# The mixers whose cuts made outside K1000 are kept (`mixer_cuts`, `carry_mixer_cuts`): the AUX
# routing (D-016), name -> (destinations a cut may touch, may a mute be kept). `aux_mix` is where
# CamillaGUI shows the AUX going into L/R: muting a ringing mic there must hold. In the `input`
# mixer only the AUX destinations, and only a lower gain: a MUTED source (or mapping) leaves its
# capture channel unused by the first mixer, which panics 5.0.0-beta.1's spectrum ring buffer
# (`_input_mixer`); K1000's restore of that is the fix, not a cut to keep.
_CUT_MIXERS: dict[str, tuple[Callable[[int], bool], bool]] = {
    AUX_MIX: (lambda dest: True, True),
    "input": (lambda dest: dest >= 2, False),
}


_SrcKey = tuple[float, bool, bool, str]          # a mixer source: gain, inverted, mute, scale


def _src_key(x: dict[str, Any]) -> _SrcKey:
    return (_num(x.get("gain")) or 0.0, bool(x.get("inverted")), bool(x.get("mute")),
            str(x.get("scale") or "dB"))


def _mapping_view(m: Any) -> dict[int, tuple[bool, dict[int, _SrcKey]]] | None:
    """{dest: (mapping muted, {channel: source key})}; None when malformed or ambiguous (a dest
    or a channel twice)."""
    out: dict[int, tuple[bool, dict[int, _SrcKey]]] = {}
    for e in _dict(m).get("mapping") or []:
        if not isinstance(e, dict) or not isinstance(e.get("dest"), int) or e["dest"] in out:
            return None
        srcs: dict[int, _SrcKey] = {}
        for x in e.get("sources") or []:
            if not isinstance(x, dict) or not isinstance(x.get("channel"), int) \
                    or x["channel"] in srcs:
                return None
            srcs[x["channel"]] = _src_key(x)
        out[e["dest"]] = (bool(e.get("mute")), srcs)
    return out


def mixer_cuts(name: str, live: Any, expected: Any) -> list[str] | None:
    """The cuts made outside K1000 that are the ONLY difference between the running mixer `name`
    and K1000's (`_CUT_MIXERS`): a source or destination muted, a dB source turned down. Returns
    their descriptions ("aux_mix 2->0 MUTED", "input 0->2 -30.0 dB": source channel -> dest),
    or None when anything else differs (another mixer, a routing, a raise, a polarity, a scale):
    that is a structure drift, rebuilt."""
    rule = _CUT_MIXERS.get(name)
    if rule is None or not isinstance(live, dict) or not isinstance(expected, dict):
        return None
    cuttable, mute_ok = rule
    lch, ech = _dict(live.get("channels")), _dict(expected.get("channels"))
    if (lch.get("in"), lch.get("out")) != (ech.get("in"), ech.get("out")):
        return None
    lmaps, emaps = _mapping_view(live), _mapping_view(expected)
    if lmaps is None or emaps is None or set(lmaps) != set(emaps):
        return None
    cuts: list[str] = []
    for dest in sorted(emaps):
        (emute, esrcs), (lmute, lsrcs) = emaps[dest], lmaps[dest]
        may = cuttable(dest)
        if set(lsrcs) != set(esrcs):
            return None
        if lmute != emute:
            if not (lmute and may and mute_ok):
                return None
            cuts.append(f"{name} ->{dest} MUTED")
        for ch in sorted(esrcs):
            (eg, einv, emu, esc), (lg, linv, lmu, lsc) = esrcs[ch], lsrcs[ch]
            if (linv, lsc) != (einv, esc):
                return None
            if lmu != emu:
                if not (lmu and may and mute_ok):
                    return None
                cuts.append(f"{name} {ch}->{dest} MUTED")
            elif not _close(lg, eg):
                if not (may and esc == "dB" and lg < eg):
                    return None
                cuts.append(f"{name} {ch}->{dest} {lg:+.1f} dB")
    return cuts


def carry_mixer_cuts(name: str, live: Any, out: Any) -> None:
    """For a restore (`control._keep_cuts`): copy into `out` (K1000's mixer `name`, edited in
    place) each cut `live` has on a source or destination the two share, under `mixer_cuts`'s
    rules (`_CUT_MIXERS`), even when the rest of the mixer differs. Then the restored mixer
    differs from K1000's by those cuts only: held, not rebuilt again."""
    rule = _CUT_MIXERS.get(name)
    if rule is None or not isinstance(live, dict) or not isinstance(out, dict):
        return
    cuttable, mute_ok = rule
    lmaps = {e.get("dest"): e for e in live.get("mapping") or [] if isinstance(e, dict)}
    for m in out.get("mapping") or []:
        dest, lm = m.get("dest"), lmaps.get(m.get("dest"))
        if not isinstance(lm, dict) or not isinstance(dest, int) or not cuttable(dest):
            continue
        if mute_ok and lm.get("mute") is True and not m.get("mute"):
            m["mute"] = True
        lsrcs = {x.get("channel"): x for x in lm.get("sources") or [] if isinstance(x, dict)}
        for s in m.get("sources") or []:
            ls = lsrcs.get(s.get("channel"))
            if not isinstance(ls, dict):
                continue
            (_lg, linv, lmu, lsc), (_g, inv, mu, sc) = _src_key(ls), _src_key(s)
            if (linv, lsc) != (inv, sc):
                continue
            if mute_ok and lmu and not mu:
                s["mute"] = True
            g, w = _num(ls.get("gain")), _num(s.get("gain"))
            if sc == "dB" and g is not None and w is not None and g < w:
                s["gain"] = g


def _block_drift(kind: str, name: str, f: Any, ef: dict[str, Any], ptr: Callable[[str, str], str],
                 skip: frozenset[str], live_sr: float, rebuild: list[str]) -> list[SetValue]:
    """One filter/processor of `config_drift`: structure problems go to `rebuild`, value
    differences come back as fixes (`ptr(name, key)` pointers)."""
    if not isinstance(f, dict):
        rebuild.append(f"{kind} {name} missing")
        return []
    if f.get("type") != ef["type"]:
        rebuild.append(f"{kind} {name} type {f.get('type')!r} != {ef['type']!r}")
        return []
    lpar, epar = _dict(f.get("parameters")), ef["parameters"]
    differ: list[str] = []
    for key, ev in epar.items():
        lv = lpar.get(key)
        if lv is None:
            rebuild.append(f"{kind} {name}.{key} missing")
        elif key == "type":
            if lv != ev:
                rebuild.append(f"{kind} {name} subtype {lv!r} != {ev!r}")
        elif ptr(name, key) in skip:
            continue
        elif ef["type"] == "Delay" and key == "delay":
            lms, ems = _delay_ms(lpar, live_sr), _delay_ms(epar, SAMPLERATE)
            if lms is None or delay_samples(lms) != delay_samples(ems):
                differ.append(key)
        elif not _close(lv, ev):
            differ.append(key)
    if any(k in _UNIT_KEYS for k in differ):
        differ = [k for k in epar if k != "type" and ptr(name, k) not in skip]
    return [SetValue(ptr(name, k), epar[k]) for k in differ]
