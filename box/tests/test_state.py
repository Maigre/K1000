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
