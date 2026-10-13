# SPDX-License-Identifier: GPL-3.0-or-later
"""State model, ranges and crash-safe persistence (no hardware, no engine)."""
import json
import math
import os

import pytest

from k1000 import state as st
from k1000.state import (
    RANGES, PeqBand, ShelfBand, State, atomic_write, load_state, range_for, save_state_atomic,
)


# ----------------------------------------------------------------------------- defaults
def test_safe_defaults():
    s = State()
    assert s.master_db == -20.0
    assert s.muted is False
    assert s.lowcut_on is True and s.lowcut_hz == 100.0
    assert s.sub_lowpass_on is False
    assert s.mono_subs is False and s.sub_inverted is False and s.top_inverted is False
    assert s.sub_delay_ms == 0.0 and s.top_delay_ms == 0.0
    assert len(s.sys_peq) == 8 and len(s.live_peq) == 4 and len(s.out_trim_db) == 4
    assert all(b.gain == 0.0 for b in s.sys_peq + s.live_peq)
    assert s.live_lowshelf.gain == 0.0 and s.live_highshelf.gain == 0.0
    assert s.out_trim_db == [0.0] * 4
    assert s.profile == "dev" and s.dev_audition == "sum"


def test_defaults_are_inside_ranges_and_stable():
    s = State()
    assert State.from_dict(s.to_dict()) == s
    assert s.clamped() == s
    for key, r in RANGES.items():
        assert r.min <= r.default <= r.max, key
        assert r.min < r.max and r.step > 0, key


def test_every_numeric_field_has_a_range():
    s = State()
    paths = ["master_db", "balance_db", "input_trim_db", "sub_delay_ms", "top_delay_ms",
             "lowcut_hz", "sub_lowpass_hz", "out_trim_db.0", "out_trim_db.3",
             "live_lowshelf.freq", "live_lowshelf.gain", "live_lowshelf.q",
             "live_highshelf.freq", "live_highshelf.gain", "live_highshelf.q"]
    paths += [f"sys_peq.{i}.{k}" for i in range(8) for k in ("freq", "gain", "q")]
    paths += [f"live_peq.{i}.{k}" for i in range(4) for k in ("freq", "gain", "q")]
    for p in paths:
        r = range_for(p)
        assert r is not None, p
        assert r.min <= s.get(p) <= r.max, p


# ----------------------------------------------------------------------------- clamping
def test_range_clamp_handles_garbage():
    r = RANGES["master_db"]
    assert r.clamp(10) == 0.0
    assert r.clamp(-500) == -80.0
    assert r.clamp(float("nan")) == -20.0
    assert r.clamp(float("inf")) == -20.0
    assert r.clamp("loud") == -20.0
    assert r.clamp(None, -30.0) == -30.0
    assert r.clamp("-12.5") == -12.5
    assert r.clamp(10 ** 400) == -20.0                  # float() overflow: a fallback, not a raise
    assert r.clamp(10 ** 400, -30.0) == -30.0


def test_from_dict_clamps_everything():
    d = State().to_dict()
    d.update(master_db=12, balance_db=-99, input_trim_db=99, sub_delay_ms=-5, top_delay_ms=1e9,
             lowcut_hz=5, sub_lowpass_hz=1e6, out_trim_db=[50, -50, "x", None],
             profile="nope", dev_audition=3, muted="yes", mono_subs=1)
    d["sys_peq"][0] = {"freq": 1e9, "gain": 99, "q": 0}
    d["live_peq"][1] = {"freq": -1, "gain": -99, "q": 1e6}
    d["live_highshelf"] = {"freq": 50, "gain": 40, "q": 9}
    s = State.from_dict(d)
    assert s.master_db == 0.0 and s.balance_db == -12.0 and s.input_trim_db == 12.0
    assert s.sub_delay_ms == 0.0 and s.top_delay_ms == 50.0
    assert s.lowcut_hz == 20.0 and s.sub_lowpass_hz == 250.0
    assert s.out_trim_db == [6.0, -24.0, 0.0, 0.0]
    assert s.profile == "dev" and s.dev_audition == "sum"
    assert s.muted is False and s.mono_subs is False      # non-bools are not truthy-coerced
    assert s.sys_peq[0] == PeqBand(20000.0, 10.0, 0.3)
    assert s.live_peq[1] == PeqBand(20.0, -20.0, 16.0)
    assert s.live_highshelf == ShelfBand(1000.0, 6.0, 1.5)


def test_from_dict_tolerates_old_and_partial():
    s = State.from_dict({"schema": 0, "master_db": -10, "sys_peq": [{"freq": 80, "gain": -3}],
                         "unknown_future_field": 1})
    assert s.master_db == -10.0
    assert len(s.sys_peq) == 8
    assert s.sys_peq[0] == PeqBand(80.0, -3.0, 1.0)
    assert s.sys_peq[1] == State().sys_peq[1]
    assert State.from_dict(None) == State()
    assert State.from_dict({"live_peq": "garbage", "live_lowshelf": [1, 2]}) == State()


def test_live_peq_boost_capped_lower_than_system():
    assert RANGES["live_peq.gain"].max <= 6.0 < RANGES["sys_peq.gain"].max


# ----------------------------------------------------------------------------- path API
def test_get_set_paths_clamp():
    s = State()
    assert s.set("master_db", 3) == 0.0
    assert s.set("live_peq.2.gain", -50) == -20.0
    assert s.live_peq[2].gain == -20.0
    assert s.set("out_trim_db.1", -3.25) == -3.25
    assert s.get("out_trim_db.1") == -3.25
    assert s.set("lowcut_on", False) is False
    assert s.set("dev_audition", "tops") == "tops"
    with pytest.raises(ValueError):
        s.set("profile", "usb")
    with pytest.raises(KeyError):
        s.set("not_a_field", 1)


def test_nudge_linear_log_fine_and_bounds():
    s = State()
    assert s.nudge("master_db", 2) == -19.0
    assert s.nudge("master_db", 1000) == 0.0
    assert s.nudge("sub_delay_ms", 1) == 0.1
    assert s.nudge("sub_delay_ms", 1, fine=True) == round(0.1 + 1 / 48, 6)   # fine = one sample
    f0 = s.live_peq[0].freq
    up = s.nudge("live_peq.0.freq", 12)          # 12 semitones = one octave
    assert math.isclose(up, 2 * f0, rel_tol=1e-3)
    assert math.isclose(s.nudge("live_peq.0.freq", -12), f0, rel_tol=1e-3)
    q0 = s.live_peq[0].q
    s.nudge("live_peq.0.q", 6)                    # 6 x 1/6 oct = x2
    assert math.isclose(s.live_peq[0].q, 2 * q0, rel_tol=1e-3)
    assert s.nudge("lowcut_hz", -1000) == 20.0
    with pytest.raises(KeyError):
        s.nudge("muted", 1)


def test_live_reset_only_touches_live_layer():
    s = State(master_db=-5.0)
    s.live_peq[1].gain = -9.0
    s.live_highshelf.gain = -4.0
    s.sys_peq[0].gain = -3.0
    r = s.live_reset()
    assert all(b.gain == 0.0 for b in r.live_peq) and r.live_highshelf.gain == 0.0
    assert r.sys_peq[0].gain == -3.0 and r.master_db == -5.0
    assert s.live_peq[1].gain == -9.0                 # original untouched


def test_copy_is_deep():
    s = State()
    c = s.copy()
    c.live_peq[0].gain = -6.0
    c.out_trim_db[0] = -1.0
    assert s.live_peq[0].gain == 0.0 and s.out_trim_db[0] == 0.0


# ----------------------------------------------------------------------------- persistence
def test_load_missing_returns_defaults(tmp_path):
    assert load_state(tmp_path / "nope.json") == State()


def test_save_load_roundtrip_and_no_tmp_left(tmp_path):
    p = tmp_path / "sub" / "state.json"          # parent created on demand
    s = State(master_db=-12.5, mono_subs=True, sub_delay_ms=3.21, profile="m4")
    s.live_peq[3] = PeqBand(3150.0, -7.5, 8.0)
    save_state_atomic(p, s)
    assert load_state(p) == s
    assert sorted(x.name for x in p.parent.iterdir()) == ["state.json"]
    assert oct(os.stat(p).st_mode & 0o777) == "0o644"
    data = json.loads(p.read_text())
    assert data["schema"] == st.SCHEMA_VERSION


def test_save_clamps(tmp_path):
    p = tmp_path / "state.json"
    s = State()
    s.master_db = 40.0                                  # bypassing set(): save must still clamp
    save_state_atomic(p, s)
    assert json.loads(p.read_text())["master_db"] == 0.0


@pytest.mark.parametrize("content", ["{not json", "", "[1, 2, 3]", "\x00\x00\x00", '"str"'])
def test_corrupt_file_falls_back_and_keeps_bad_copy(tmp_path, content):
    p = tmp_path / "state.json"
    p.write_text(content)
    assert load_state(p) == State()
    bad = tmp_path / "state.json.bad"
    assert bad.exists() and bad.read_text() == content


def test_binary_garbage_file(tmp_path):
    p = tmp_path / "state.json"
    p.write_bytes(b"\xff\xfe\x00garbage\x80")
    assert load_state(p) == State()
    assert (tmp_path / "state.json.bad").read_bytes() == b"\xff\xfe\x00garbage\x80"


def test_atomic_write_failure_keeps_old_content(tmp_path, monkeypatch):
    p = tmp_path / "active.yml"
    atomic_write(p, "old\n")

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write(p, "new\n")
    assert p.read_text() == "old\n"
    assert sorted(x.name for x in tmp_path.iterdir()) == ["active.yml"]


def test_atomic_write_fsyncs_file_and_dir(tmp_path, monkeypatch):
    calls = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (calls.append(fd), real(fd)))
    atomic_write(tmp_path / "x.json", b"{}")
    assert len(calls) == 2                      # the data file, then the directory


# ----------------------------------------------------------------------------- review fixes
@pytest.mark.parametrize("content", [
    '{"schema": 1, "master_db": ' + "9" * 400 + "}",          # OverflowError in float()
    "[" * 100000 + "]" * 100000,                               # RecursionError in json.loads
    '{"schema": 1, "live_peq": [{"freq": ' + "9" * 400 + "}]}",
])
def test_load_state_never_raises_on_hostile_files(tmp_path, content):
    p = tmp_path / "state.json"
    p.write_text(content)
    s = load_state(p)
    assert isinstance(s, State)
    assert s.master_db == State().master_db
    assert (tmp_path / "state.json.bad").exists() or s == State()


def test_delay_samples_mirrors_engine_rounding():
    # measured on 5.0.0-beta.1 (subsample off): 0.0104 ms -> 0, 0.0105 -> 1, 0.031249 -> 1,
    # 0.03125 -> 2 (half up), 0.020833 -> 1, 0.104 -> 5, 1.009 -> 48
    for ms, n in ((0.0104, 0), (0.0105, 1), (0.031249, 1), (0.03125, 2), (0.020833, 1),
                  (0.104, 5), (1.009, 48), (1.0, 48), (50.0, 2400), (-1.0, 0), ("x", 0), (None, 0)):
        assert st.delay_samples(ms) == n, ms


def test_fine_delay_steps_are_exactly_one_sample_each():
    s = State()
    seen = [st.delay_samples(s.sub_delay_ms)]
    for _ in range(2400):                                # 0 -> 50 ms one sample at a time
        s.nudge("sub_delay_ms", 1, fine=True)
        seen.append(st.delay_samples(s.sub_delay_ms))
    assert seen == list(range(2401))                     # no dead and no double detent, no drift
    s.sub_delay_ms = 0.3                                 # off-grid start (coarse steps): still +1 each
    n0 = st.delay_samples(0.3)
    for k in range(1, 50):
        s.nudge("sub_delay_ms", 1, fine=True)
        assert st.delay_samples(s.sub_delay_ms) == n0 + k
    s.sub_delay_ms = 0.0
    for k in range(1, 30):                               # coarse 0.1 ms never lands on the same sample
        before = st.delay_samples(s.sub_delay_ms)
        s.nudge("sub_delay_ms", 1)
        assert st.delay_samples(s.sub_delay_ms) - before in (4, 5)


# ----------------------------------------------------------------------------- D-011
def test_lowcut_moves_2_hz_per_click_1_hz_fine_on_its_grid():
    """D-011 (Thomas: "LO-CUT is moving 6 Hz per click, I'd like 2 Hz"): linear, 2 Hz a click,
    1 Hz with the push held, 20-300 Hz, default 100; every other frequency stays log."""
    r = RANGES["lowcut_hz"]
    assert (r.min, r.max, r.step, r.fine, r.default, r.log, r.snap) == (20.0, 300.0, 2.0, 1.0, 100.0,
                                                                        False, True)
    for key in ("sub_lowpass_hz", "sys_peq.freq", "live_peq.freq", "live_lowshelf.freq",
                "live_highshelf.freq"):
        assert RANGES[key].log and not RANGES[key].snap, key
    s = State()
    assert [s.nudge("lowcut_hz", 1) for _ in range(3)] == [102.0, 104.0, 106.0]
    assert s.nudge("lowcut_hz", -3) == 100.0
    assert s.nudge("lowcut_hz", 1, fine=True) == 101.0         # odd values: only with the push
    assert s.nudge("lowcut_hz", 1) == 102.0                    # back on the 2 Hz grid, +1 Hz
    s.lowcut_hz = 101.0
    assert s.nudge("lowcut_hz", -1) == 100.0
    # the acceleration's x4 (input_map: LO-CUT accel 4 on fast spins only) = 8 Hz, on the grid
    assert s.nudge("lowcut_hz", 4) == 108.0 and s.nudge("lowcut_hz", -4) == 100.0
    # the ends
    s.lowcut_hz = 299.0
    assert s.nudge("lowcut_hz", 1) == 300.0 and s.nudge("lowcut_hz", 1) == 300.0
    s.lowcut_hz = 21.0
    assert s.nudge("lowcut_hz", -1) == 20.0 and s.nudge("lowcut_hz", -4) == 20.0
    assert s.nudge("lowcut_hz", 1000) == 300.0 and s.nudge("lowcut_hz", -1000) == 20.0


@pytest.mark.parametrize("start,detents,fine,want", [
    (112.25, 1, False, 114.0),       # the box's saved value: one click up = the next grid line
    (112.25, -1, False, 112.0),      # one click down = the grid line below (never 2.25 Hz)
    (112.25, 4, False, 120.0),       # a fast spin (x4) from off the grid
    (112.25, -4, False, 106.0),
    (112.25, 1, True, 113.0),        # fine: the 1 Hz grid
    (112.25, -1, True, 112.0),
    (113.0, 1, False, 114.0),        # an odd value (set with fine) rejoins the 2 Hz grid
    (113.0, -1, False, 112.0),
    (112.0, 1, False, 114.0),        # on the grid: whole steps
    (112.0, -1, False, 110.0),
    (299.5, 1, False, 300.0),
    (20.5, -1, False, 20.0),
])
def test_lowcut_off_grid_value_snaps_on_its_first_move(start, detents, fine, want):
    s = State()
    s.lowcut_hz = start
    assert s.nudge("lowcut_hz", detents, fine) == want


def test_lowcut_off_grid_value_is_never_rewritten_at_load(tmp_path):
    """No snapping at load: an engine change at boot is exactly what must not happen."""
    p = tmp_path / "state.json"
    s = State.from_dict({"lowcut_hz": 112.25})
    assert s.lowcut_hz == 112.25 and s.clamped().lowcut_hz == 112.25
    save_state_atomic(p, s)
    assert json.loads(p.read_text())["lowcut_hz"] == 112.25
    assert load_state(p).lowcut_hz == 112.25


def test_align_delay_is_sub_minus_top_and_not_a_stored_field():
    """D-011: ALIGN E3 is one signed delay over the two stored ones (state.json, dspconfig and
    CamillaGUI keep sub_delay_ms and top_delay_ms)."""
    assert State(sub_delay_ms=2.35).get(st.ALIGN_DELAY) == 2.35
    assert State(top_delay_ms=1.25).get(st.ALIGN_DELAY) == -1.25
    assert State(sub_delay_ms=1.0, top_delay_ms=0.25).get(st.ALIGN_DELAY) == 0.75
    assert State().get(st.ALIGN_DELAY) == 0.0
    assert st.ALIGN_DELAY not in RANGES and st.ALIGN_DELAY not in State().to_dict()
    r = range_for(st.ALIGN_DELAY)
    assert r is not None and (r.min, r.max, r.step, r.fine) == (-50.0, 50.0, 0.1, st.SAMPLE_MS)
    assert State.from_dict({**State().to_dict(), st.ALIGN_DELAY: 9.0}) == State()   # ignored key


@pytest.mark.parametrize("value,sub,top", [
    (2.0, 2.0, 0.0), (-1.5, 0.0, 1.5), (0.0, 0.0, 0.0), (-0.0, 0.0, 0.0),
    (99.0, 50.0, 0.0), (-99.0, 0.0, 50.0),
])
def test_align_delay_set_writes_only_the_delayed_side(value, sub, top):
    s = State(sub_delay_ms=3.0, top_delay_ms=1.0)
    s.set(st.ALIGN_DELAY, value)
    assert (s.sub_delay_ms, s.top_delay_ms) == (sub, top)
    assert "-0.0" not in json.dumps(s.to_dict())


def test_align_delay_garbage_keeps_the_current_value():
    s = State(top_delay_ms=1.25)
    for bad in (float("nan"), float("inf"), "x", None):
        assert s.set(st.ALIGN_DELAY, bad) == -1.25
        assert (s.sub_delay_ms, s.top_delay_ms) == (0.0, 1.25)


def test_align_delay_stops_at_zero_before_crossing():
    """Crossing zero takes a click on exactly 0.00 (ALIGNED): the operator feels it."""
    s = State(sub_delay_ms=0.2)
    assert s.nudge(st.ALIGN_DELAY, -1) == 0.1
    assert s.nudge(st.ALIGN_DELAY, -1) == 0.0 and (s.sub_delay_ms, s.top_delay_ms) == (0.0, 0.0)
    assert s.nudge(st.ALIGN_DELAY, -1) == -0.1 and (s.sub_delay_ms, s.top_delay_ms) == (0.0, 0.1)
    assert s.nudge(st.ALIGN_DELAY, -1) == -0.2
    assert s.nudge(st.ALIGN_DELAY, 5) == 0.0                   # a coalesced turn stops at 0 too
    assert s.nudge(st.ALIGN_DELAY, 5) == 0.5 and (s.sub_delay_ms, s.top_delay_ms) == (0.5, 0.0)
    s = State(sub_delay_ms=0.05)                               # off the 0.1 grid (fine steps)
    assert s.nudge(st.ALIGN_DELAY, -1) == 0.0
    s = State(top_delay_ms=st.SAMPLE_MS)                        # one sample: fine crosses too
    assert s.nudge(st.ALIGN_DELAY, 1, fine=True) == 0.0
    assert s.nudge(st.ALIGN_DELAY, 1, fine=True) == round(st.SAMPLE_MS, 6)
    assert st.delay_samples(s.sub_delay_ms) == 1 and s.top_delay_ms == 0.0
    s = State(sub_delay_ms=50.0)
    assert s.nudge(st.ALIGN_DELAY, 1) == 50.0                  # the ends clamp
    s = State(top_delay_ms=50.0)
    assert s.nudge(st.ALIGN_DELAY, -1) == -50.0


def test_align_delay_normalises_a_both_sides_state_on_its_first_move_only():
    """A saved state with both delays set (v0's SETUP > Top delay + sub delay) shows the net
    value; loading keeps both (no engine change at boot); the first move writes one side."""
    loaded = State.from_dict({"sub_delay_ms": 1.0, "top_delay_ms": 0.5})
    assert (loaded.sub_delay_ms, loaded.top_delay_ms) == (1.0, 0.5)
    assert loaded.get(st.ALIGN_DELAY) == 0.5
    s = loaded.copy()
    assert s.nudge(st.ALIGN_DELAY, 1) == 0.6 and (s.sub_delay_ms, s.top_delay_ms) == (0.6, 0.0)
    s = State(sub_delay_ms=0.3, top_delay_ms=1.0)               # net -0.7: the tops are later
    assert s.nudge(st.ALIGN_DELAY, -1) == -0.8 and (s.sub_delay_ms, s.top_delay_ms) == (0.0, 0.8)
    s = State(sub_delay_ms=1.0, top_delay_ms=1.0)               # net 0: moves off zero freely
    assert s.nudge(st.ALIGN_DELAY, -1) == -0.1 and (s.sub_delay_ms, s.top_delay_ms) == (0.0, 0.1)


def test_align_delay_walk_never_delays_both_sides_nor_jumps_across_zero():
    import random
    rnd = random.Random(11)
    for _trial in range(40):
        s = State(sub_delay_ms=round(rnd.uniform(0, 5), 3), top_delay_ms=round(rnd.uniform(0, 5), 3))
        prev = s.get(st.ALIGN_DELAY)
        for _ in range(200):
            d = rnd.choice((-3, -1, -1, 1, 1, 3))
            v = s.nudge(st.ALIGN_DELAY, d, fine=rnd.random() < 0.3)
            assert s.sub_delay_ms == 0.0 or s.top_delay_ms == 0.0
            assert s.sub_delay_ms >= 0.0 and s.top_delay_ms >= 0.0
            assert v == s.get(st.ALIGN_DELAY) and -50.0 <= v <= 50.0
            assert not (prev > 0 > v or prev < 0 < v), (prev, d, v)    # 0 in between, always
            prev = v
