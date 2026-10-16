# SPDX-License-Identifier: GPL-3.0-or-later
"""The golden cases for the Sunday-safety rule (2026-10-16, AUX / loudness / de-harsh).

With aux_mode "off", loud_on False and deharsh_on False, `dspconfig.generate` must write
byte for byte the YAML that HEAD f2671b0 (the build Thomas ran his first rig test on) wrote for
the same state. The fixtures in `fixtures/dspconfig-f2671b0/` were written by
`write_golden()` running on a `git archive f2671b0` copy of box/k1000, BEFORE the features
existed. Only fields that exist at f2671b0 are used here, so the same module builds the same
states on both sides.

The fixtures are the reference, not a snapshot: regenerate them only from f2671b0 itself, never
from a later tree (that would make the identity test pass by definition):

    git archive f2671b0 box/k1000 | tar -x -C "$TMP" && python tests/dspconfig_golden.py "$TMP/box"
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

GOLDEN_REV = "f2671b0"
GOLDEN_DIR = Path(__file__).resolve().parent / "fixtures" / f"dspconfig-{GOLDEN_REV}"


def _extreme(State: Any) -> Any:
    """test_dspconfig.extreme_state (every value at an end of its range)."""
    s = State(master_db=0.0, balance_db=12.0, input_trim_db=12.0, sub_delay_ms=50.0,
              top_delay_ms=50.0, lowcut_hz=300.0, sub_lowpass_on=True, sub_lowpass_hz=40.0,
              sub_inverted=True, top_inverted=True, out_trim_db=[6.0, -24.0, 6.0, -24.0])
    for b in s.sys_peq:
        b.freq, b.gain, b.q = 20000.0, 10.0, 16.0
    for b in s.live_peq:
        b.freq, b.gain, b.q = 20.0, -20.0, 0.3
    s.live_lowshelf.gain, s.live_highshelf.gain = 6.0, -12.0
    return s


def _extreme_mono(State: Any) -> Any:
    s = _extreme(State)
    s.mono_subs = True
    return s


def _tuned(State: Any) -> Any:
    """A realistic show state: a tune, a live cut switched OUT (D-014), a delay, mono subs."""
    s = State(master_db=-14.5, balance_db=-2.0, input_trim_db=-3.0, sub_delay_ms=2.354167,
              lowcut_hz=112.25, sub_lowpass_on=True, sub_lowpass_hz=95.0, mono_subs=True)
    s.sys_peq[1].gain, s.sys_peq[1].q = -4.5, 2.5
    s.live_peq[2].freq, s.live_peq[2].gain, s.live_peq[2].q = 2700.0, -6.0, 8.0
    s.live_peq[3].gain, s.live_peq[3].on = -3.0, False
    s.live_highshelf.gain = -2.0
    s.out_trim_db[0] = -1.5
    return s


# name -> state factory (takes the State class of the build under test)
STATES: dict[str, Callable[[Any], Any]] = {
    "default": lambda State: State(),
    "default_m4": lambda State: State(profile="m4"),
    "mono": lambda State: State(mono_subs=True),
    "extreme": _extreme,
    "extreme_mono": _extreme_mono,
    "tops_audition": lambda State: State(mono_subs=True, sub_inverted=True, top_inverted=True,
                                         lowcut_on=False, dev_audition="tops"),
    "tuned": _tuned,
}

# name -> (profile name, k1000.yaml-style overrides)
PROFILES: dict[str, tuple[str, dict[str, Any]]] = {
    "dev": ("dev", {}),
    "m4": ("m4", {}),
    "m4_6ch": ("m4", {"profiles": {"m4": {"capture_channels": 6}}}),
    "m4_in12": ("m4", {"profiles": {"m4": {"input_map": [0, 1]}}}),
}


def cases() -> list[tuple[str, str]]:
    return [(p, s) for p in PROFILES for s in STATES]


def golden_path(profile_key: str, state_key: str) -> Path:
    return GOLDEN_DIR / f"{profile_key}__{state_key}.yml"


def render(dspconfig: Any, profiles: Any, State: Any, profile_key: str, state_key: str) -> str:
    name, ov = PROFILES[profile_key]
    prof = profiles.load_profile(name, ov or None, probe=False)
    return dspconfig.to_yaml(dspconfig.generate(prof, STATES[state_key](State)))


def params_path() -> Path:
    return GOLDEN_DIR / "param_map.json"


def render_params(dspconfig: Any, State: Any) -> str:
    """{state: param_map(state)} as JSON: the owned pointers `diff_ops`, adoption and drift use."""
    import json

    return json.dumps({k: dspconfig.param_map(f(State)) for k, f in STATES.items()}, indent=1,
                      sort_keys=True) + "\n"


def write_golden(box_dir: str) -> list[Path]:
    """Write every fixture from the k1000 package found in `box_dir` (a HEAD copy)."""
    import importlib
    import sys

    sys.path.insert(0, box_dir)
    try:
        dc = importlib.import_module("k1000.dspconfig")
        pf = importlib.import_module("k1000.profiles")
        st = importlib.import_module("k1000.state")
        assert str(Path(dc.__file__).resolve()).startswith(str(Path(box_dir).resolve())), dc.__file__
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        out = []
        for p, s in cases():
            path = golden_path(p, s)
            path.write_text(render(dc, pf, st.State, p, s), encoding="utf-8")
            out.append(path)
        params_path().write_text(render_params(dc, st.State), encoding="utf-8")
        out.append(params_path())
        return out
    finally:
        sys.path.remove(box_dir)


if __name__ == "__main__":
    import sys as _sys

    for _p in write_golden(_sys.argv[1]):
        print(_p)
