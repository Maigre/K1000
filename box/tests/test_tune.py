# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for k1000.tune (the Friday tune import, docs/06-phone.md §11): the filter-list parser on
real exports, the plan onto the 8 system bands, the bandwidth conversion, and TuneDesk's
preview / apply / undo against the REAL Controller over an in-memory engine.

The REW samples are real exports, transcribed from public posts (filter values, headers and
the "-0.00" gain kept exactly; REW's column padding restored where the post had collapsed it):
- REW V5.19, "Equaliser: Generic", 12 filters (AVNirvana REW forum, 2020): more than 8 -> refused.
- REW V5.20.13, "500hz-eq" (3 filters) and "1-6-design-curve" (7 filters, one at -0.00 dB)
  (Roon community forum, 2023).
- REW V4.00, "Equaliser: DSP1124P", PA filters with BW/60 and the nominal-frequency brackets
  (the example in REW's own help, "File menu": Export filter settings as text).
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from k1000 import tune
from k1000.state import DEFAULT_SYS_PEQ_FREQS, RANGES, State
from k1000.tune import TuneDesk, parse_filters, plan_bands, q_for_bandwidth
from k1000.ui import eqgraph as eg

from test_web import boot, make_ctl, run

REW_519_12 = """\
Filter Settings file

Room EQ V5.19
Dated: Jan 14, 2020 7:00:46 PM

Notes:

Equaliser: Generic
Average 1
Filter  1: ON  PK       Fc   365 Hz  Gain -13.5 dB  Q  1.000
Filter  2: ON  PK       Fc   1003 Hz  Gain  -9.7 dB  Q  1.000
Filter  3: ON  PK       Fc   15057 Hz  Gain -11.1 dB  Q  1.154
Filter  4: ON  PK       Fc   7336 Hz  Gain -10.2 dB  Q  1.389
Filter  5: ON  PK       Fc   175 Hz  Gain -12.5 dB  Q  2.000
Filter  6: ON  PK       Fc   10338 Hz  Gain  -2.2 dB  Q  1.000
Filter  7: ON  PK       Fc   2423 Hz  Gain  -5.8 dB  Q  2.498
Filter  8: ON  PK       Fc   70.9 Hz  Gain  -4.7 dB  Q  2.126
Filter  9: ON  PK       Fc   219 Hz  Gain   7.0 dB  Q  5.000
Filter 10: ON  PK       Fc   120 Hz  Gain   6.8 dB  Q  7.359
Filter 11: ON  PK       Fc   343 Hz  Gain  -3.6 dB  Q  4.707
Filter 12: ON  PK       Fc   1594 Hz  Gain   3.3 dB  Q  4.983
Filter 13: ON  None
Filter 14: ON  None
Filter 15: ON  None
Filter 16: ON  None
Filter 17: ON  None
Filter 18: ON  None
Filter 19: ON  None
Filter 20: ON  None
"""

REW_520_500HZ = """\
Filter Settings file

Room EQ V5.20.13
Dated: Feb 19, 2023 5:04:04 PM

Notes:500hz-eq

Equaliser: Generic
RMS Average 1
Filter  1: ON  PK       Fc   26.05 Hz  Gain  -9.00 dB  Q  4.331
Filter  2: ON  PK       Fc   111.5 Hz  Gain   5.70 dB  Q  3.238
Filter  3: ON  PK       Fc   118.5 Hz  Gain -11.50 dB  Q  4.911
Filter  4: ON  None
Filter  5: ON  None
Filter  6: ON  None
"""

# the same post's other export, as the forum showed it (whitespace collapsed)
REW_520_DESIGN = """\
Filter Settings file
Room EQ V5.20.13
Dated: Feb 19, 2023 10:02:20 AM
Notes:1-6-design-curve
Equaliser: Generic
RMS Average 1
Filter 1: ON PK Fc 26.00 Hz Gain -0.00 dB Q 9.864
Filter 2: ON PK Fc 26.95 Hz Gain -13.60 dB Q 2.874
Filter 3: ON PK Fc 30.65 Hz Gain 6.00 dB Q 2.000
Filter 4: ON PK Fc 108.5 Hz Gain 4.50 dB Q 3.418
Filter 5: ON PK Fc 118.5 Hz Gain -9.70 dB Q 4.677
Filter 6: ON PK Fc 1125 Hz Gain -2.00 dB Q 2.815
Filter 7: ON PK Fc 3852 Hz Gain -5.00 dB Q 1.311
"""

REW_400_DSP1124P = """\
Filter Settings file

Room EQ V4.00
Dated: 07-Jan-2007 17:20:32

Notes:Example filter settings

Equaliser: DSP1124P
sampledata.txt
Bass limited 80Hz 12dB/Octave
Target level: 75.0dB
Filter 1: ON  PA   Fc   129.1Hz (  125 +2 )   Gain -18.5dB  BW/60  4.0
Filter 2: ON  PA   Fc    36.8Hz (   40 -7 )   Gain -15.5dB  BW/60 10.0
Filter 3: ON  PA   Fc    99.1Hz (  100 -1 )   Gain  -3.5dB  BW/60  1.0
Filter 4: ON  None
Filter 5: ON  None
Filter 6: ON  None
Filter 7: ON  None
Filter 8: ON  None
Filter 9: ON  None
Filter 10: ON  None
Filter 11: ON  None
Filter 12: ON  None
"""

# EqualizerAPO / AutoEQ (ParametricEQ.txt) form, the lines of EqualizerAPO's own reference
APO = """\
Preamp: -6.5 dB
Filter 1: ON PK Fc 50.0 Hz Gain -10.0 dB Q 2.50
Filter 2: ON PEQ Fc 100 Hz Gain 1.0 dB BW Oct 0.167
Filter 3: ON Modal Fc 100 Hz Gain 3.0 dB Q 5.41 T60 target 100 ms
Filter 4: ON LSC 10.8 dB Fc 300 Hz Gain 5.0 dB
Filter 5: ON HS 12dB Fc 500 Hz Gain 5.0 dB
Filter 6: ON HP Fc 30.0 Hz
Filter 7: ON NO Fc 800 Hz
Filter 8: ON AP Fc 900 Hz Q 0.707
Filter: ON PK Fc 1250 Hz Gain -2.5 dB Q 3.00
GraphicEQ: 25 6; 40 4.5; 63 3
"""


def _f(imp: tune.Import) -> list[tuple[float, float, float]]:
    return [(f.freq, f.gain, f.q) for f in imp.filters]


# ======================================================================= the parser
def test_rew_generic_export_reads_every_filter_and_ignores_the_rest():
    imp = parse_filters(REW_520_500HZ)
    assert imp.ok and imp.error == "" and imp.skipped == [] and imp.notes == []
    assert _f(imp) == [(26.05, -9.0, 4.331), (111.5, 5.7, 3.238), (118.5, -11.5, 4.911)]
    assert [f.label for f in imp.filters] == ["Filter 1", "Filter 2", "Filter 3"]
    assert imp.filters[0].line == 10


def test_rew_export_with_a_minus_zero_gain_skips_that_filter():
    imp = parse_filters(REW_520_DESIGN)
    assert imp.ok
    assert _f(imp) == [(26.95, -13.6, 2.874), (30.65, 6.0, 2.0), (108.5, 4.5, 3.418),
                       (118.5, -9.7, 4.677), (1125.0, -2.0, 2.815), (3852.0, -5.0, 1.311)]
    assert imp.skipped == ["Filter 1: gain 0 dB, no effect: skipped"]


def test_twelve_filters_are_refused_whole():
    imp = parse_filters(REW_519_12)
    assert not imp.ok and len(imp.filters) == 12
    assert imp.error.startswith("12 peaking filters: the system EQ has 8 bands")
    assert "Nothing applied" in imp.error and "8 filters at most" in imp.error


def test_old_rew_pa_filters_with_bw60_and_nominal_brackets():
    imp = parse_filters(REW_400_DSP1124P)
    assert imp.ok and [f.freq for f in imp.filters] == [129.1, 36.8, 99.1]
    assert [f.gain for f in imp.filters] == [-18.5, -15.5, -3.5]
    # BW/60 4.0 = 1/15 oct -> Q 21.6: beyond the system's 16, clamped and said so
    assert imp.filters[0].q == 16.0 and imp.filters[2].q == 16.0
    assert imp.filters[1].q == pytest.approx(q_for_bandwidth(10 / 60, 36.8), abs=1e-3)
    assert any(n.startswith("Filter 1: BW/60 4.0 (0.067 oct) = Q 21.6") for n in imp.notes)
    assert "Filter 1: Q 21.6 -> 16 (system range 0.3..16)" in imp.notes
    assert "Filter 3: Q 86.6 -> 16 (system range 0.3..16)" in imp.notes


def test_equalizer_apo_lines_peaking_kept_the_rest_listed_as_skipped():
    imp = parse_filters(APO)
    assert imp.ok
    assert [f.label for f in imp.filters] == ["Filter 1", "Filter 2", "Filter 3", "line 10"]
    assert _f(imp)[0] == (50.0, -10.0, 2.5) and _f(imp)[2] == (100.0, 3.0, 5.41)
    assert imp.filters[1].q == pytest.approx(q_for_bandwidth(0.167, 100.0), abs=1e-3)
    assert _f(imp)[3] == (1250.0, -2.5, 3.0)
    assert imp.skipped == [
        "Filter 4: LSC (low shelf): skipped (peaking filters only)",
        "Filter 5: HS (high shelf): skipped (peaking filters only)",
        "Filter 6: HP (high-pass): skipped (peaking filters only)",
        "Filter 7: NO (notch): skipped (peaking filters only)",
        "Filter 8: AP (all-pass): skipped (peaking filters only)",
        "line 11: GraphicEQ: skipped (peaking filters only)",
    ]
    assert imp.notes[0].startswith("line 1: Preamp -6.5 dB ignored")


def test_values_beyond_the_system_ranges_are_clamped_and_listed():
    imp = parse_filters("Filter 1: ON PK Fc 25000 Hz Gain 12 dB Q 30\n"
                        "Filter 2: ON PK Fc 12 Hz Gain -25 dB Q 0.1\n")
    assert imp.ok and _f(imp) == [(20000.0, 10.0, 16.0), (20.0, -20.0, 0.3)]
    assert imp.notes == [
        "Filter 1: Fc 25000 Hz -> 20000 Hz (system range 20..20000 Hz)",
        "Filter 1: gain +12.0 dB -> +10.0 dB (system range -20.0..+10.0 dB)",
        "Filter 1: Q 30 -> 16 (system range 0.3..16)",
        "Filter 2: Fc 12 Hz -> 20 Hz (system range 20..20000 Hz)",
        "Filter 2: gain -25.0 dB -> -20.0 dB (system range -20.0..+10.0 dB)",
        "Filter 2: Q 0.1 -> 0.3 (system range 0.3..16)",
    ]
    # the same ranges the knobs and the engine use
    assert (RANGES["sys_peq.freq"].max, RANGES["sys_peq.gain"].max, RANGES["sys_peq.q"].max) == \
        (20000.0, 10.0, 16.0)


def test_decimal_commas_unicode_minus_and_khz():
    """A French REW writes decimal commas; a web page writes U+2212."""
    text = ("Filter  1: ON  PK       Fc   26,05 Hz  Gain  −9,00 dB  Q  4,331\n"
            "Filter 2: ON PK Fc 2.44 kHz Gain -3.5 dB Q 8.65\n"
            "Filter 5: ON PK Fc 2,500 Hz Gain -1 dB Q 2\n"
            "Bass limited 80Hz 12dB/Octave\n")
    imp = parse_filters(text)
    assert imp.ok
    assert _f(imp) == [(26.05, -9.0, 4.331), (2440.0, -3.5, 8.65), (2500.0, -1.0, 2.0)]
    assert imp.notes == ["Filter 5: Fc 2,500 read as 2500 Hz"] and imp.skipped == []


def test_free_lines_in_a_hand_typed_list():
    """Open Sound Meter has no export: its filters are typed, in any loose form (no 'Filter N:'
    line anywhere in the paste)."""
    text = ("125 Hz -4 dB Q 5\n"
            "PEQ 4: 2,5 kHz, −3,5 dB, Q 4\n"
            "LS 100 Hz 3 dB Q 0.7\n"
            "Bass limited 80Hz 12dB/Octave\n"
            "1 kHz +2 dB BW Oct 0.5\n")
    imp = parse_filters(text)
    assert imp.ok
    assert _f(imp)[:2] == [(125.0, -4.0, 5.0), (2500.0, -3.5, 4.0)]
    assert _f(imp)[2][:2] == (1000.0, 2.0)
    assert imp.filters[2].q == pytest.approx(q_for_bandwidth(0.5, 1000.0), abs=1e-3)
    assert [f.label for f in imp.filters] == ["line 1", "line 2", "line 5"]
    assert imp.skipped == ["line 3: LS (low shelf): skipped (peaking filters only)"]


# A real REW V5.20 header, its Notes line written by the operator (free text: REW never reads
# it), then a real 8-filter export (REW_520_DESIGN's values and one more).
REW_NOTES_8 = """\
Filter Settings file

Room EQ V5.20.13
Dated: Feb 19, 2023 10:02:20 AM

Notes:boost 50 Hz +4 dB Q 1 for the DJ, sub crossover 80 Hz, -3 dB, Q 0.7

Equaliser: Generic
RMS Average 1
Filter  1: ON  PK       Fc   26.95 Hz  Gain -13.60 dB  Q  2.874
Filter  2: ON  PK       Fc   30.65 Hz  Gain   6.00 dB  Q  2.000
Filter  3: ON  PK       Fc   108.5 Hz  Gain   4.50 dB  Q  3.418
Filter  4: ON  PK       Fc   118.5 Hz  Gain  -9.70 dB  Q  4.677
Filter  5: ON  PK       Fc   1125 Hz  Gain  -2.00 dB  Q  2.815
Filter  6: ON  PK       Fc   3852 Hz  Gain  -5.00 dB  Q  1.311
Filter  7: ON  PK       Fc   26.00 Hz  Gain  -0.00 dB  Q  9.864
Filter  8: ON  PK       Fc   250.0 Hz  Gain  -1.50 dB  Q  4.000
Filter  9: ON  None
Filter 10: ON  None
"""


def test_a_rew_notes_line_with_hz_db_and_q_is_never_a_filter():
    """LAS-1: REW's free-text Notes (and any other non-'Filter' line of an export) once became
    a filter on band 1 (a +4 dB boost here), pushed the real filters down a band and made a
    valid 8-filter export count 9 (refused)."""
    imp = parse_filters(REW_NOTES_8)
    assert imp.ok and imp.error == ""
    assert [f.label for f in imp.filters] == [f"Filter {i}" for i in (1, 2, 3, 4, 5, 6, 8)]
    assert _f(imp)[0] == (26.95, -13.6, 2.874) and _f(imp)[-1] == (250.0, -1.5, 4.0)
    assert all(f.gain <= 6.0 for f in imp.filters) and not any(f.freq == 50.0 for f in imp.filters)
    assert plan_bands(imp.filters)[0].freq == 26.95                 # band 1 is REW's Filter 1
    assert imp.notes == ['line 6: "Notes:boost 50 Hz +4 dB Q 1 for the DJ, sub crossover 80 Hz…" '
                         'ignored (a header or notes line, never a filter)']
    # 8 real filters (none at 0 dB) and the same notes: applied whole, not refused as 9
    eight = REW_NOTES_8.replace("Gain  -0.00 dB  Q  9.864", "Gain  -1.00 dB  Q  9.864")
    imp8 = parse_filters(eight)
    assert imp8.ok and len(imp8.filters) == 8
    assert [f.freq for f in imp8.filters][:1] == [26.95]


def test_free_lines_next_to_filter_lines_are_listed_never_applied():
    text = ("Filter 1: ON PK Fc 2.44 kHz Gain -3.5 dB Q 8.65\n"
            "remember: 125 Hz -4 dB Q 5 sounded better\n"
            "Equaliser: Generic 63 Hz +6 dB Q 2\n"
            "Average 1\n")
    imp = parse_filters(text)
    assert imp.ok and _f(imp) == [(2440.0, -3.5, 8.65)]
    assert imp.notes == [
        'line 2: "remember: 125 Hz -4 dB Q 5 sounded better" ignored (not a "Filter N:" line; '
        'free lines are read only in a list without Filter lines)',
        'line 3: "Equaliser: Generic 63 Hz +6 dB Q 2" ignored (a header or notes line, never a '
        'filter)']
    # the REW header keys are never filters, even in a hand-typed list
    assert not parse_filters("Notes: 50 Hz +4 dB Q 1\nDated: 1 kHz -3 dB Q 2\n").ok


def test_off_filters_none_slots_comments_and_missing_values():
    text = ("# a comment\n; another\nFilter 1: OFF PK Fc 50 Hz Gain -3 dB Q 2\n"
            "Filter 2: ON None\nFilter 3: ON PK Fc 100 Hz Q 2\nFilter 4: ON PK Gain -3 dB Q 2\n"
            "Filter 5: ON PK Fc 200 Hz Gain -3 dB\nFilter 6: ON PK Fc 300 Hz Gain -3 dB Q 0\n"
            "Filter 7: ON PK Fc 400 Hz Gain -3 dB Q 2\n")
    imp = parse_filters(text)
    assert imp.ok and _f(imp) == [(400.0, -3.0, 2.0)]
    assert imp.skipped == ["Filter 1: PK switched OFF in the list: skipped",
                           "Filter 3: no Gain: skipped", "Filter 4: no Fc (frequency): skipped",
                           "Filter 5: no Q or bandwidth: skipped",
                           "Filter 6: no Q or bandwidth: skipped"]


@pytest.mark.parametrize("text, why", [
    (None, "nothing pasted"),
    (42, "nothing pasted"),
    ("", "no peaking filter found"),
    ("Filter Settings file\nRoom EQ V5.20.13\n", "no peaking filter found"),
    ("Filter 1: ON LS Fc 100 Hz Gain 3 dB\n", "no peaking filter found"),
    ("x" * (tune.MAX_TEXT + 1), "too long"),
    ("\n" * (tune.MAX_LINES + 1), "too many lines"),
])
def test_nothing_to_apply_is_said(text: Any, why: str):
    imp = parse_filters(text)
    assert not imp.ok and imp.error.startswith(why)


def test_hostile_text_never_raises():
    for text in ("Filter 1: ON PK Fc 1e999 Hz Gain 1e999 dB Q 1e999", "Filter 1: ON PK Fc nan Hz",
                 "Filter 999999999999999999999: ON PK Fc 100 Hz Gain -3 dB Q 99999999999",
                 "\x00\x01Filter 1: ON PK Fc 100 Hz Gain -3 dB Q 2", "Filter 1: ON PK Fc 0 Hz Gain -3 dB Q 2",
                 "Fc Gain Q Hz dB", "Filter 1: ON PK Fc 100 Hz Gain -3 dB BW Oct 0",
                 "Filter 1: ON PK Fc 100 Hz Gain -3 dB BW Oct 99"):
        imp = parse_filters(text)
        for f in imp.filters:
            assert all(math.isfinite(v) for v in (f.freq, f.gain, f.q))
            assert RANGES["sys_peq.freq"].min <= f.freq <= RANGES["sys_peq.freq"].max
            assert RANGES["sys_peq.q"].min <= f.q <= RANGES["sys_peq.q"].max
        json.dumps(imp.to_dict(), allow_nan=False)


def test_a_bandwidth_becomes_the_q_of_the_same_digital_filter():
    """EqualizerAPO's 'BW Oct' (and CamillaDSP's Peaking/Bandwidth) == our Peaking with that Q,
    at every frequency, not only where the analog relation holds."""
    f = np.geomspace(20, 20000, 400)
    for fc in (40.0, 1000.0, 8000.0, 15000.0):
        for bw in (0.1, 1 / 3, 1.0):
            q = q_for_bandwidth(bw, fc)
            a = eg.response_db(eg.Band(eg.PEAKING_BW, fc, -6.0, bw), f)
            b = eg.response_db(eg.Band(eg.PEAKING, fc, -6.0, q), f)
            assert np.max(np.abs(a - b)) < 1e-6, (fc, bw)
    assert q_for_bandwidth(1 / 3, 100.0) == pytest.approx(eg.q_for_octaves(1 / 3), rel=1e-3)


def test_plan_puts_the_filters_on_bands_1_to_n_and_the_rest_flat_at_home():
    imp = parse_filters(REW_520_500HZ)
    bands = plan_bands(imp.filters)
    assert len(bands) == 8
    assert [(b.freq, b.gain, b.q) for b in bands[:3]] == _f(imp)
    for i in range(3, 8):
        assert (bands[i].freq, bands[i].gain, bands[i].q) == (DEFAULT_SYS_PEQ_FREQS[i], 0.0, 1.0)
    # the preview curve is the engine's: the cut is there, the flat bands add nothing
    c = tune.curve(bands)
    i = int(np.argmin(np.abs(np.array(c["f"]) - 118.5)))
    assert min(c["db"]) < -8 and c["db"][i] < -5 and max(c["db"]) < 3
    assert len(c["f"]) == len(c["db"]) == 160


# ======================================================================= the desk
def test_desk_preview_changes_nothing(tmp_path: Path):
    async def go() -> None:
        ctl, eng = make_ctl(tmp_path)
        await boot(ctl)
        desk = TuneDesk(ctl, tmp_path / "tune-undo.json")
        before = ctl.system_eq()
        p = desk.preview(REW_520_500HZ)
        assert p["ok"] and p["bands"][0] == {"freq": 26.05, "gain": -9.0, "q": 4.331}
        assert p["current"] == before and len(p["curve"]["db"]) == len(p["curve_now"]["db"])
        assert max(abs(v) for v in p["curve_now"]["db"]) < 1e-6          # a flat system EQ
        bad = desk.preview(REW_519_12)
        assert not bad["ok"] and bad["bands"] is None and bad["curve"] is None
        assert bad["why"].startswith("12 peaking filters")
        assert ctl.system_eq() == before
        json.dumps(p, allow_nan=False)
        await ctl.stop()
    run(go())


def test_desk_apply_goes_through_the_controller_saves_and_undoes(tmp_path: Path):
    async def go() -> None:
        st = State()
        st.sys_peq[0].gain, st.sys_peq[0].freq = -3.0, 80.0          # an earlier tune
        ctl, eng = make_ctl(tmp_path, st)
        await boot(ctl)
        undo_file = tmp_path / "tune-undo.json"
        desk = TuneDesk(ctl, undo_file)
        old = ctl.system_eq()
        res = await desk.apply(REW_520_DESIGN)
        assert res["ok"] and res["saved"] is True and res["undo"] == old
        assert ctl.system_eq()[1] == {"freq": 30.65, "gain": 6.0, "q": 2.0}
        assert ctl.system_eq()[6] == {"freq": 4000.0, "gain": 0.0, "q": 1.0}     # band 7 flat, home
        # the engine has it (click-safe parameter writes, no pipeline change) ...
        p = eng.cfg["filters"]["sys_peq_1"]["parameters"]
        assert (p["freq"], p["gain"], p["q"]) == (26.95, -13.6, 2.874)
        assert eng.cfg["filters"]["sys_peq_5"]["parameters"]["gain"] == -2.0
        # ... and the disk too
        saved = json.loads((tmp_path / "state.json").read_text())["sys_peq"]
        assert saved[0] == {"freq": 26.95, "gain": -13.6, "q": 2.874}
        assert json.loads(undo_file.read_text())["undo"] == old
        # a k1000 restart keeps the undo
        assert TuneDesk(ctl, undo_file).undo_bands == old
        # a refused import changes nothing, not even the undo
        bad = await desk.apply(REW_519_12)
        assert not bad["ok"] and desk.undo_bands == old
        # undo: the earlier tune back, once
        u = await desk.undo()
        assert u["ok"] and u["saved"] and ctl.system_eq() == old and not undo_file.exists()
        assert eng.cfg["filters"]["sys_peq_1"]["parameters"]["gain"] == -3.0
        assert (await desk.undo()) == {"ok": False, "why": "nothing to undo"}
        await ctl.stop()
    run(go())


def test_set_system_eq_is_clamped_and_keeps_what_it_cannot_read(tmp_path: Path):
    async def go() -> None:
        ctl, eng = make_ctl(tmp_path)
        await boot(ctl)
        prev = ctl.set_system_eq([{"freq": 99999, "gain": 50, "q": 0.0001},
                                  {"freq": "x", "gain": None, "q": float("nan")}, None, "junk"])
        assert prev[0] == {"freq": 63.0, "gain": 0.0, "q": 1.0}
        eq = ctl.system_eq()
        assert eq[0] == {"freq": 20000.0, "gain": 10.0, "q": 0.3}
        assert eq[1] == {"freq": 125.0, "gain": 0.0, "q": 1.0}           # unreadable: kept
        assert ctl.set_system_eq("not a list") == eq and ctl.system_eq() == eq
        assert await ctl.settle(timeout=3.0)
        assert eng.cfg["filters"]["sys_peq_1"]["parameters"]["gain"] == 10.0
        await ctl.stop()
    run(go())
