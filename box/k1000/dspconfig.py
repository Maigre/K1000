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
"""
from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass
from typing import Any, Iterable, NamedTuple, Union

import yaml

from .profiles import LOGICAL_NAMES, Profile
from .state import (
    LOWCUT_OFF_HZ, LOWCUT_Q, N_LIVE_PEQ, N_OUT, N_SYS_PEQ, RANGES, SAMPLE_MS, SAMPLERATE,
    SUB_LOWPASS_OFF_HZ, SUB_LOWPASS_Q, PeqBand, ShelfBand, State, delay_samples, effective_gain,
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
INPUT_CHAIN: tuple[str, ...] = ("in_trim", *SYS_PEQ, *LIVE_PEQ, "live_lowshelf", "live_highshelf")
SUB_CHAIN: tuple[str, ...] = ("sub_trim", "sub_lowpass", "sub_delay", "sub_fader")
TOP_CHAIN: tuple[str, ...] = ("top_trim", "top_lowcut", "top_delay", "top_fader")
MIXERS: tuple[str, ...] = ("input", "split", "output")


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


def param_map(state: State) -> dict[str, Any]:
    """Every config value K1000 owns, as {JSON pointer: value}, from the clamped state.

    Delays are snapped to whole samples, so two states whose delays round to the same sample
    count map to the same value and `diff_ops` emits nothing (no needless delay-line rebuild).
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


def _src(channel: int, gain: float = 0.0) -> dict[str, Any]:
    return {"channel": channel, "gain": gain, "inverted": False, "scale": "dB"}


def _alsa(device: str, channels: int, fmt: str | None) -> dict[str, Any]:
    d: dict[str, Any] = {"type": "Alsa", "channels": channels, "device": device}
    if fmt:
        d["format"] = fmt   # None = CamillaDSP picks the best format the device offers
    return d


def _input_mixer(profile: Profile) -> dict[str, Any]:
    """Program L/R from `input_map`. Every other capture channel joins L at linear gain 0
    (adds exactly nothing): CamillaDSP leaves a channel no mixer source uses empty, and
    5.0.0-beta.1's spectrum ring buffer then indexes that empty channel and panics the capture
    thread (the M4's mic inputs 1-2, 2026-10-16: the engine restarted every 2 s)."""
    l, r = profile.input_map
    pad = [{"channel": c, "gain": 0.0, "inverted": False, "scale": "linear"}
           for c in range(profile.capture_channels) if c not in (l, r)]
    return {
        "description": f"program L/R from capture channels {l}, {r}",
        "labels": ["L", "R"],
        "channels": {"in": profile.capture_channels, "out": 2},
        "mapping": [{"dest": 0, "sources": [_src(l)] + pad}, {"dest": 1, "sources": [_src(r)]}],
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
    """The full CamillaDSP v5 config (a dict ready for `to_yaml` / `json.dumps`)."""
    s = state.clamped()
    pm = param_map(s)

    def v(name: str, key: str) -> Any:
        return pm[_pp(name, key)]

    filters: dict[str, Any] = {"in_trim": _gain(v("in_trim", "gain"), description="input trim")}
    for name in SYS_PEQ + LIVE_PEQ:
        filters[name] = _biquad("Peaking", freq=v(name, "freq"), gain=v(name, "gain"), q=v(name, "q"))
    filters["live_lowshelf"] = _biquad("Lowshelf", freq=v("live_lowshelf", "freq"),
                                       gain=v("live_lowshelf", "gain"), q=v("live_lowshelf", "q"))
    filters["live_highshelf"] = _biquad("Highshelf", freq=v("live_highshelf", "freq"),
                                        gain=v("live_highshelf", "gain"), q=v("live_highshelf", "q"))
    filters["sub_trim"] = _gain(0.0, v("sub_trim", "inverted"), "subs polarity")
    filters["sub_lowpass"] = _biquad("Lowpass", f"sub low-pass (off = {SUB_LOWPASS_OFF_HZ:g} Hz)",
                                     freq=v("sub_lowpass", "freq"), q=SUB_LOWPASS_Q)
    filters["sub_delay"] = _delay(v("sub_delay", "delay"))
    filters["sub_fader"] = _volume("Aux1", "subs group (balance +b/2)")
    filters["top_trim"] = _gain(0.0, v("top_trim", "inverted"), "tops polarity")
    filters["top_lowcut"] = _biquad("Highpass", f"tops Lo-Cut 12 dB/oct (off = {LOWCUT_OFF_HZ:g} Hz)",
                                    freq=v("top_lowcut", "freq"), q=LOWCUT_Q)
    filters["top_delay"] = _delay(v("top_delay", "delay"))
    filters["top_fader"] = _volume("Aux2", "tops group (balance -b/2)")
    for i, name in enumerate(OUT_TRIM):
        filters[name] = _gain(v(name, "gain"), description=f"output trim {LOGICAL_NAMES[i]}")

    pipeline: list[dict[str, Any]] = [
        {"type": "Mixer", "name": "input", "description": "program L/R"},
        {"type": "Filter", "channels": [0, 1], "names": list(INPUT_CHAIN),
         "description": "trim, system PEQ, live EQ"},
        {"type": "Mixer", "name": "split", "description": "2 -> 4: Sub L/R, Top L/R"},
        {"type": "Filter", "channels": [0, 1], "names": list(SUB_CHAIN), "description": "subs"},
        {"type": "Filter", "channels": [2, 3], "names": list(TOP_CHAIN), "description": "tops"},
    ]
    for i, name in enumerate(OUT_TRIM):
        pipeline.append({"type": "Filter", "channels": [i], "names": [name],
                         "description": f"trim {LOGICAL_NAMES[i]}"})
    pipeline.append({"type": "Mixer", "name": "output", "description": "to physical outputs"})

    return {
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
        "mixers": {
            "input": _input_mixer(profile),
            "split": _split_mixer(s.mono_subs),
            "output": _output_mixer(profile, s.dev_audition),
        },
        "filters": filters,
        "pipeline": pipeline,
    }


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
    controller keeps `state.profile` equal to the running profile).
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


def _is_trim(ptr: str) -> bool:
    parts = _unpointer(ptr)
    return len(parts) == 4 and parts[0] == "filters" and (parts[1] == "in_trim" or parts[1] in OUT_TRIM)


def smooth_ops(old_state: State, new_state: State, *, max_db_step: float = 0.5,
               max_trim_step: float = 0.1, max_oct_step: float = 1.0 / 12.0,
               max_steps: int = 100) -> Plan:
    """`diff_ops`, re-planned so no single engine write is big enough to click.

    Returns [(wait_before_s, ops), ...]: sleep, then send that batch (one PatchConfig when it
    holds several SetValue). Biquad gains/frequencies/Q move in steps of at most `max_db_step`
    dB / `max_oct_step` octave (the engine smooths each Biquad switch); Gain-filter trims, which
    the engine applies as an instant step, use `max_trim_step`. Steps are `STEP_WAIT_S` apart,
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


class Drift(NamedTuple):
    """What differs between the running config and what K1000 would generate (`config_drift`).

    devices: the `devices` block differs: any scalar (volume_limit, volume_ramp_time_ms,
        target_level, enable_rate_adjust, samplerate, chunksize), a capture/playback key, or a
        device option K1000 leaves unset (resampler, silence_threshold...). Measured on
        5.0.0-beta.1: ANY devices change, even volume_limit alone via SetConfigValue/PatchConfig,
        logs "Devices changed, restart required" and stops/restarts capture and playback, i.e.
        an audio drop. Never fix it automatically while audio runs: show it, and upload
        `expected` from SETUP (with confirm) or while muted at boot. A hidden Main above range
        is handled meanwhile by `fader_drift`.
    rebuild: mixers / pipeline / a filter's presence, type, subtype or a missing key (e.g. Q
        replaced by bandwidth) differ: a whole-config upload rebuilds the pipeline (possible
        click). Without a device restart only if `devices` is empty; otherwise upload
        `with_live_devices(expected, live)` (measured: "Rebuilding pipeline" only, no device
        restart, devices drift still reported) and leave the device fix for SETUP.
    fixes: owned/fixed parameter values that differ while the structure matches: send them as
        ONE PatchConfig(ops_to_patch(fixes)) (no restart, no rebuild). Empty when `rebuild` is
        not (the upload carries them). A filter whose unit/scale key differs gets ALL its
        parameters in `fixes`, so value and unit change together.
    """

    devices: list[str]
    rebuild: list[str]
    fixes: list[SetValue]

    @property
    def clean(self) -> bool:
        return not (self.devices or self.rebuild or self.fixes)


_DEVICE_SIDES = ("capture", "playback")
_UNIT_KEYS = ("delay_unit", "scale")   # change the meaning of the other values of the filter


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

    Returns a `Drift(devices, rebuild, fixes)`; see `Drift` for what each one costs and when the
    controller may act on it. Call it after `state_from_config` so owned edits are adopted, not
    reverted. Delays compare as whole samples (the engine's resolution), so a GUI value like
    1.009 ms against K1000's 1.0 ms (both 48 samples) is not a difference: a write would
    rebuild the delay line and click for nothing. `skip_pointers`: values K1000 wrote in the
    last ~0.2 s (a stale read must not trigger a redundant write).
    """
    live = _as_cfg(live_cfg)
    skip = frozenset(skip_pointers)
    devices: list[str] = []
    rebuild: list[str] = []
    ld, ed = _dict(live.get("devices")), _dict(expected.get("devices"))
    _device_drift(ld, ed, "devices.", devices)
    for side in _DEVICE_SIDES:
        _device_drift(_dict(ld.get(side)), _dict(ed.get(side)), f"devices.{side}.", devices)
    for name in MIXERS:
        if _norm_mixer(_dict(live.get("mixers")).get(name)) != _norm_mixer(expected["mixers"][name]):
            rebuild.append(f"mixer {name} differs")
    lpipe = live.get("pipeline")
    lp = [_norm_step(st) for st in (lpipe if isinstance(lpipe, list) else [])]
    ep = [_norm_step(st) for st in expected.get("pipeline") or []]
    if lp != ep:
        rebuild.append("pipeline differs")

    live_sr = _num(ld.get("samplerate")) or SAMPLERATE
    fixes: list[SetValue] = []
    lf = _dict(live.get("filters"))
    for name, ef in (expected.get("filters") or {}).items():
        f = lf.get(name)
        if not isinstance(f, dict):
            rebuild.append(f"filter {name} missing")
            continue
        if f.get("type") != ef["type"]:
            rebuild.append(f"filter {name} type {f.get('type')!r} != {ef['type']!r}")
            continue
        lpar, epar = _dict(f.get("parameters")), ef["parameters"]
        differ: list[str] = []
        for key, ev in epar.items():
            lv = lpar.get(key)
            if lv is None:
                rebuild.append(f"filter {name}.{key} missing")
            elif key == "type":
                if lv != ev:
                    rebuild.append(f"filter {name} subtype {lv!r} != {ev!r}")
            elif _pp(name, key) in skip:
                continue
            elif ef["type"] == "Delay" and key == "delay":
                lms, ems = _delay_ms(lpar, live_sr), _delay_ms(epar, SAMPLERATE)
                if lms is None or delay_samples(lms) != delay_samples(ems):
                    differ.append(key)
            elif not _close(lv, ev):
                differ.append(key)
        if any(k in _UNIT_KEYS for k in differ):
            differ = [k for k in epar if k != "type" and _pp(name, k) not in skip]
        fixes += [SetValue(_pp(name, k), epar[k]) for k in differ]
    return Drift(devices, rebuild, [] if rebuild else fixes)
