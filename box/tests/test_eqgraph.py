# SPDX-License-Identifier: GPL-3.0-or-later
"""k1000.ui.eqgraph: the SHOW page's EQ curve must be the engine's sound, and cheap to draw.

Reference values: the RBJ analytic points (a peak reads its gain at f0, a shelf half its gain
at f0, a 2nd-order Butterworth -3.01 dB at f0) and the very assertions of CamillaDSP
v5.0.0-beta.1's own biquad tests (src/filters/biquad.rs, 44.1 kHz), so a drift between this
module and the engine's `from_config` shows up here.
"""
from __future__ import annotations

import cmath
import math
import statistics
import time

import numpy as np
import pytest
from PIL import Image

from k1000 import state as st
from k1000.ui import eqgraph as eg
from k1000.ui.eqgraph import (HIGHPASS, HIGHSHELF, LOWPASS, LOWSHELF, PEAKING, PEAKING_BW, Band)


def db_at(band: Band, f: float, fs: float = eg.FS) -> float:
    return float(eg.response_db(band, [f], fs)[0])


def direct_db(band: Band, f: float, fs: float) -> float:
    """|H(e^jw)| from the coefficients the slow way (independent of the phi formula)."""
    b0, b1, b2, a1, a2 = eg.coefficients(band, fs)
    z1 = cmath.exp(-1j * 2 * math.pi * f / fs)
    h = (b0 + b1 * z1 + b2 * z1 * z1) / (1 + a1 * z1 + a2 * z1 * z1)
    return 20 * math.log10(abs(h))


# ------------------------------------------------------------------------------ filters
@pytest.mark.parametrize("f0,gain,q", [(1000.0, 6.0, 4.0), (2520.0, -6.5, 8.0), (63.0, -20.0, 16.0),
                                       (15000.0, 6.0, 0.3), (250.0, -3.0, 1.0)])
def test_peaking_reads_its_gain_at_f0(f0, gain, q):
    assert db_at(Band(PEAKING, f0, gain, q), f0) == pytest.approx(gain, abs=1e-6)


def test_peaking_is_flat_far_away_and_exactly_flat_at_0db():
    b = Band(PEAKING, 1000.0, -12.0, 4.0)
    assert abs(db_at(b, 20.0)) < 0.05 and abs(db_at(b, 20000.0)) < 0.2
    flat = Band(PEAKING, 1000.0, 0.0, 8.0)
    assert flat.flat and not np.any(eg.response_db(flat, np.geomspace(20, 20000, 50)))
    # 0 dB really is unity in the engine too: b == a
    b0, b1, b2, a1, a2 = eg.coefficients(Band(PEAKING, 1000.0, 1e-12, 8.0))
    assert (b1, b2) == pytest.approx((a1, a2)) and b0 == pytest.approx(1.0)


def test_peaking_q_is_rbj_bandwidth():
    """Q from q_for_octaves(bw): half the gain (in dB) at f0 * 2^(+-bw/2) (RBJ's BW definition),
    at 1 kHz where the digital warping is negligible."""
    for bw in (1 / 3, 1 / 6, 1 / 10):
        q = eg.q_for_octaves(bw)
        b = Band(PEAKING, 1000.0, -12.0, q)
        for edge in (1000.0 * 2 ** (bw / 2), 1000.0 * 2 ** (-bw / 2)):
            assert db_at(b, edge) == pytest.approx(-6.0, abs=0.05)
    assert eg.q_for_octaves(1 / 3) == pytest.approx(4.318, abs=1e-3)
    assert eg.NOTCH_Q == pytest.approx(8.651, abs=1e-3)
    assert eg.q_for_octaves(1 / 10) == pytest.approx(14.42, abs=0.01)


@pytest.mark.parametrize("gain", [6.0, -12.0, 2.0])
@pytest.mark.parametrize("q", [0.4, 0.707, 1.5])
def test_shelves_full_gain_at_the_far_end_half_at_f0(gain, q):
    ls = Band(LOWSHELF, 120.0, gain, q)
    assert db_at(ls, 1e-3) == pytest.approx(gain, abs=1e-6)
    assert db_at(ls, 120.0) == pytest.approx(gain / 2, abs=1e-9)
    assert abs(db_at(ls, 20000.0)) < 0.01
    hs = Band(HIGHSHELF, 8000.0, gain, q)
    assert db_at(hs, eg.FS / 2 * 0.999999) == pytest.approx(gain, abs=1e-4)
    assert db_at(hs, 8000.0) == pytest.approx(gain / 2, abs=1e-9)
    assert abs(db_at(hs, 20.0)) < 0.01


def test_lowcut_is_12db_butterworth():
    hp = Band(HIGHPASS, st.RANGES["lowcut_hz"].default, q=st.LOWCUT_Q)
    f0 = hp.freq
    assert db_at(hp, f0) == pytest.approx(-3.01, abs=0.02)
    assert db_at(hp, f0 / 2) == pytest.approx(-12.3, abs=0.1)     # 10 log10(1 + 2^4)
    assert abs(db_at(hp, 20 * f0)) < 0.01
    # 'off' parks it at 10 Hz: one octave above, a 2nd-order Butterworth is 10 log10(16/17)
    off = Band(HIGHPASS, st.LOWCUT_OFF_HZ, q=st.LOWCUT_Q)
    assert db_at(off, 20.0) == pytest.approx(-0.26, abs=0.02)
    assert abs(db_at(off, 40.0)) < 0.02


# The assertions of CamillaDSP v5.0.0-beta.1 src/filters/biquad.rs tests, at 44.1 kHz.
def test_camilladsp_parity_lowpass_highpass_peaking():
    fs = 44100.0
    lp = Band(LOWPASS, 100.0, q=1 / math.sqrt(2))
    assert db_at(lp, 100.0, fs) == pytest.approx(-3.0, abs=0.1)
    assert db_at(lp, 10.0, fs) == pytest.approx(0.0, abs=0.1)
    assert db_at(lp, 400.0, fs) == pytest.approx(-24.0, abs=0.2)
    hp = Band(HIGHPASS, 100.0, q=1 / math.sqrt(2))
    assert db_at(hp, 100.0, fs) == pytest.approx(-3.0, abs=0.1)
    assert db_at(hp, 25.0, fs) == pytest.approx(-24.0, abs=0.2)
    assert db_at(hp, 400.0, fs) == pytest.approx(0.0, abs=0.1)
    pk = Band(PEAKING, 100.0, 7.0, 3.0)
    assert db_at(pk, 100.0, fs) == pytest.approx(7.0, abs=0.1)
    assert db_at(pk, 25.0, fs) == pytest.approx(0.0, abs=0.1)
    assert db_at(pk, 400.0, fs) == pytest.approx(0.0, abs=0.1)


def test_camilladsp_parity_shelves_via_slope():
    fs = 44100.0
    q = eg.shelf_q_from_slope(6.0, -24.0)
    hs = Band(HIGHSHELF, 100.0, -24.0, q)
    assert db_at(hs, 100.0, fs) == pytest.approx(-12.0, abs=0.1)
    assert db_at(hs, 200.0, fs) == pytest.approx(-18.0, abs=1.0)
    assert db_at(hs, 50.0, fs) == pytest.approx(-6.0, abs=1.0)
    assert db_at(hs, 1.0, fs) == pytest.approx(0.0, abs=0.1)
    assert db_at(hs, 10000.0, fs) == pytest.approx(-24.0, abs=0.1)
    ls = Band(LOWSHELF, 100.0, -24.0, q)
    assert db_at(ls, 200.0, fs) == pytest.approx(-6.0, abs=1.0)
    assert db_at(ls, 50.0, fs) == pytest.approx(-18.0, abs=1.0)
    assert db_at(ls, 1.0, fs) == pytest.approx(-24.0, abs=0.1)
    # biquad.rs lowshelf_slope_vs_q: slope 12 == q 1/sqrt(2), whatever the gain
    assert eg.shelf_q_from_slope(12.0, -24.0) == pytest.approx(1 / math.sqrt(2))
    assert eg.coefficients(Band(LOWSHELF, 100.0, -24.0, eg.shelf_q_from_slope(12.0, -24.0)), fs) == \
        pytest.approx(eg.coefficients(Band(LOWSHELF, 100.0, -24.0, 1 / math.sqrt(2)), fs))


@pytest.mark.parametrize("band", [Band(PEAKING, 2520.0, -6.5, 8.0), Band(LOWSHELF, 120.0, 4.0, 0.5),
                                  Band(HIGHSHELF, 9000.0, -8.0, 1.2), Band(HIGHPASS, 85.0, q=0.707),
                                  Band(LOWPASS, 120.0, q=0.707), Band(PEAKING_BW, 2000.0, 3.0, 1 / 3)])
def test_phi_form_matches_direct_evaluation(band):
    for f in (20.0, 63.0, 250.0, 1000.0, 2520.0, 8000.0, 16000.0, 19999.0):
        assert db_at(band, f) == pytest.approx(direct_db(band, f, eg.FS), abs=1e-6)


def test_curve_is_the_sum_in_db_and_skips_flat_bands():
    bands = [Band(PEAKING, 2520.0, -6.5, 8.0), Band(LOWSHELF, 120.0, 2.0, 0.707),
             Band(PEAKING, 1000.0, 0.0, 4.0)]
    f = np.geomspace(20, 20000, 200)
    total = eg.curve_db(bands, f)
    assert np.allclose(total, eg.response_db(bands[0], f) + eg.response_db(bands[1], f))
    assert eg.curve_db([], f).shape == f.shape and not eg.curve_db([], f).any()


def test_live_bands_follow_state_slots_and_tolerate_garbage():
    s = st.State()
    s.live_peq[1].freq, s.live_peq[1].gain, s.live_peq[1].q = 2520.0, -6.5, 8.0
    s.live_highshelf.gain = -2.0
    b = eg.live_bands(s)
    assert [x.kind for x in b] == list(eg.LIVE_KINDS)
    assert b[1] == Band(PEAKING, 2520.0, -6.5, 8.0)
    assert b[5].kind == HIGHSHELF and b[5].gain == -2.0
    assert eg.live_bands(s.to_dict())[1] == b[1]       # the dict form (phone/JSON) too
    for junk in (None, {}, {"live_peq": [None, {"freq": "x"}]}, object()):
        bb = eg.live_bands(junk)
        assert len(bb) == 6 and all(x.flat for x in bb)
    assert np.allclose(eg.curve_db(eg.live_bands(st.State()), np.geomspace(20, 20000, 30)), 0.0)


def test_geq_bands_match_camilladsp_graphic_equalizer():
    """README: 31 bands on 20 Hz-20 kHz -> 1/3 octave, first centres 22.4, 27.9, 34.9 Hz, last two
    14.3 and 17.9 kHz; bands at 0 dB are dropped (the engine skips them too)."""
    g = [1.0] * 31
    bands = eg.geq_bands(g)
    assert [round(b.freq, 1) for b in bands[:3]] == [22.4, 27.9, 34.9]
    assert round(bands[-2].freq / 1000, 1) == 14.3 and round(bands[-1].freq / 1000, 1) == 17.9
    assert bands[0].q == pytest.approx(math.log2(1000) / 31)
    g[5] = 0.0
    assert len(eg.geq_bands(g)) == 30
    one = eg.geq_bands([0.0] * 15 + [-6.0] + [0.0] * 15)
    assert db_at(one[0], one[0].freq) == pytest.approx(-6.0, abs=1e-9)


# ------------------------------------------------------------------------------ axes
def test_log_axis_maps_decades_evenly_and_round_trips():
    ax = eg.LogAxis(0, 479)
    assert ax.x(20.0) == pytest.approx(0.0) and ax.x(20000.0) == pytest.approx(479.0)
    assert ax.x(200.0) == pytest.approx(479 / 3) and ax.x(2000.0) == pytest.approx(2 * 479 / 3)
    assert ax.x(math.sqrt(20 * 20000)) == pytest.approx(479 / 2)
    for f in (20.0, 63.0, 1000.0, 2520.0, 19999.0):
        assert ax.f(ax.x(f)) == pytest.approx(f)
    xs, fs = ax.columns(2)
    assert xs[0] == 0 and fs[0] == pytest.approx(20.0) and np.all(np.diff(fs) > 0)
    assert np.allclose(ax.x(fs), xs)


def test_db_axis_clamps_to_the_box():
    da = eg.DbAxis(124, 253, 9.0, -21.0)
    assert da.y(9.0) == 124 and da.y(-21.0) == 253
    assert da.y(30.0) == 124 and da.y(-60.0) == 253
    assert da.y(0.0) == pytest.approx(124 + 129 * 9 / 30)
    assert np.allclose(da.y(np.array([9.0, -21.0, -99.0])), [124, 253, 253])


# ------------------------------------------------------------------------------ RTA
F = 10.0 * 2000.0 ** (np.arange(265) / 264)          # what GetSpectrum returns for 10 Hz-20 kHz, 265 bins


def music(rng, ring_hz=None, ring_db=16.0):
    m = -50.0 - 3.0 * np.log2(F / 1000.0) + rng.normal(0, 1.5, len(F))
    if ring_hz:
        m = np.maximum(m, m[np.argmin(abs(F - ring_hz))] + ring_db - 240 * np.abs(np.log2(F / ring_hz)))
    return m


def test_rta_falls_at_its_rate_and_holds_peaks():
    r = eg.Rta(fall_db_s=24.0, hold_s=3.0)
    loud, quiet = np.full(len(F), -20.0), np.full(len(F), -80.0)
    r.update(F, loud, 0.0)
    r.update(F, quiet, 0.5)
    assert np.allclose(r.level, -32.0)                   # 0.5 s x 24 dB/s
    r.update(F, quiet, 1.0)
    assert np.allclose(r.level, -44.0)
    assert np.allclose(r.hold, -20.0)                    # held...
    r.update(F, quiet, 3.2)
    assert np.allclose(r.hold, -80.0)                    # ...for 3 s, then released
    fr = r.frame("MIC")
    assert fr is not None and fr.source == "MIC" and fr.hi_db - fr.lo_db == pytest.approx(r.span_db)


def test_rta_peak_needs_a_narrow_persistent_peak():
    rng = np.random.default_rng(0)
    r = eg.Rta(persist_s=0.7)
    t = 0.0
    for _ in range(6):                                    # 0.5 s: not yet
        r.update(F, music(rng, 2470.0), t)
        t += 0.1
    assert r.peak_hz is None
    for _ in range(4):
        r.update(F, music(rng, 2470.0), t)
        t += 0.1
    assert r.peak_hz is not None and abs(math.log2(r.peak_hz / 2470.0)) < 1 / 24
    assert r.grab_target() == (r.peak_hz, "PEAK") and r.grab_hz() == r.peak_hz
    # plain music (broad, no ring): no peak; GRAB falls back to the loudest bin in range
    r2 = eg.Rta()
    for k in range(20):
        r2.update(F, music(rng), k * 0.1)
    assert r2.peak_hz is None
    hz, why = r2.grab_target()
    assert why == "LOUDEST" and 100.0 <= hz <= 16000.0


def test_rta_peak_that_wanders_is_not_a_peak():
    rng = np.random.default_rng(1)
    r = eg.Rta(persist_s=0.7)
    for k, hz in enumerate(np.geomspace(500, 4000, 15)):  # a sweep: moves 1/5 octave per frame
        r.update(F, music(rng, float(hz)), k * 0.1)
        assert r.peak_hz is None


def test_rta_silence_gives_no_grab_target():
    r = eg.Rta()
    r.update(F, np.full(len(F), -200.0), 0.0)
    assert r.grab_hz() is None and r.peak_hz is None


def test_combine_power_is_a_power_average():
    a = np.array([-20.0, -40.0])
    assert np.allclose(eg.combine_power(a, a), a)
    assert eg.combine_power([-20.0], [-200.0])[0] == pytest.approx(-23.01, abs=0.01)


def test_free_band_never_picks_a_band_in_use():
    b = eg.live_bands(st.State())
    assert eg.free_band(b) == 0 and eg.free_band(b, prefer=2) == 2
    used = [Band(PEAKING, 1000.0, -3.0, 4.0)] * 4 + b[4:]
    assert eg.free_band(used) is None
    mixed = [used[0], b[1], used[2], b[3]] + b[4:]
    assert eg.free_band(mixed, prefer=0) == 1             # the selected band is in use
    assert eg.free_band(mixed, prefer=4) == 1             # shelves are never GRAB targets


# ------------------------------------------------------------------------------ drawing
def demo_bands():
    s = st.State()
    s.live_peq[1].freq, s.live_peq[1].gain, s.live_peq[1].q = 2520.0, -6.5, 8.0
    s.live_peq[3].freq, s.live_peq[3].gain = 6300.0, -3.0
    s.live_lowshelf.gain = 2.0
    return eg.live_bands(s)


def demo_rta():
    rng = np.random.default_rng(2)
    r = eg.Rta()
    for k in range(10):
        r.update(F, music(rng, 2470.0), k * 0.1)
    return r


BOX = (0, 124, 480, 254)


def test_draw_graph_puts_markers_where_the_bands_are():
    img = Image.new("RGB", (480, 320))
    geo = eg.draw_graph(img, BOX, demo_bands(), 1, demo_rta().frame())
    mx, my = geo["markers"][1]
    assert mx == pytest.approx(geo["faxis"].x(2520.0)) and my == pytest.approx(geo["daxis"].y(-6.5))
    assert img.getpixel((int(mx), int(my) - 9)) == eg.STYLE.sel_marker      # amber disc
    assert set(geo["markers"]) == {0, 1, 2, 3, 4, 5}
    assert geo["peak_x"] is not None
    for x, y in geo["markers"].values():                  # all inside the box
        assert BOX[0] <= x < BOX[2] and BOX[1] <= y < BOX[3]
    # nothing drawn outside the box
    assert img.crop((0, 0, 480, BOX[1])).getbbox() is None
    assert img.crop((0, BOX[3], 480, 320)).getbbox() is None


def test_peak_is_a_small_cyan_pointer_and_the_db_scale_is_labelled_at_the_right():
    """D-009: the PEAK is a small cyan pointer (the frequency is written by the page), not a
    line down the graph; the 0 dB line is labelled; the dB labels sit at the right edge with a
    black outline, so they read over the spectrum."""
    img = Image.new("RGB", (480, 320))
    geo = eg.draw_graph(img, BOX, demo_bands(), 1, demo_rta().frame())
    a = np.asarray(img)
    cyan = (a == np.array(eg.STYLE.peak, np.uint8)).all(axis=2)
    x = int(round(geo["peak_x"]))
    assert eg.STYLE.peak == (0, 200, 255) and not eg.STYLE.peak_line
    assert cyan[BOX[1]:BOX[1] + 12, x - 8:x + 9].sum() > 20
    assert cyan[BOX[1] + 13:BOX[3]].sum() == 0
    fill = np.array(eg.STYLE.label_db_fill, np.uint8)
    for db in (6.0, 0.0, -6.0, -12.0):
        y = round(geo["daxis"].y(db))
        box = a[y + 2:y + 20, 420:478]
        assert (box == fill).all(axis=2).sum() > 8, db           # the label
        assert (box == 0).all(axis=2).sum() > 30, db             # its outline (and black)
    left = a[BOX[1]:BOX[3], 0:30]                                # nothing at the left edge
    assert (left == fill).all(axis=2).sum() == 0


def test_draw_graph_extremes_and_no_rta():
    s = st.State()
    s.live_peq[0].freq, s.live_peq[0].gain, s.live_peq[0].q = 20000.0, -20.0, 16.0
    s.live_peq[1].freq, s.live_peq[1].gain, s.live_peq[1].q = 20.0, 6.0, 0.3
    s.live_lowshelf.gain, s.live_highshelf.gain = -12.0, 6.0
    for sel in (None, 0, 1, 4, 5):
        img = eg.render_graph((480, 130), eg.live_bands(s), sel, None)
        assert img.size == (480, 130)
    eg.render_graph((480, 130), eg.live_bands(s), 1, None, eq_in=False)
    eg.render_graph((200, 60), [], None, demo_rta().frame())


def test_flat_eq_draws_a_straight_0db_line():
    img = eg.render_graph((480, 130), eg.live_bands(st.State()), None, None)
    y0 = round(eg.DbAxis(0, 129, eg.STYLE.top_db, eg.STYLE.bottom_db).y(0.0))
    row = np.asarray(img)[y0]
    assert (row == eg.STYLE.curve).all(axis=1).mean() > 0.8   # white across (markers aside)


def test_graph_render_is_fast():
    """Pi 4 measured 4.9 ms median for the whole graph with RTA (2026-10-12); here a loose bound
    so a regression that multiplies the cost (e.g. per-frame grid drawing) fails anywhere."""
    img = Image.new("RGB", (480, 320))
    bands, fr = demo_bands(), demo_rta().frame()
    for _ in range(5):
        eg.draw_graph(img, BOX, bands, 1, fr)
    times = []
    for _ in range(30):
        t0 = time.perf_counter()
        eg.draw_graph(img, BOX, bands, 1, fr)
        times.append((time.perf_counter() - t0) * 1000)
    med = statistics.median(times)
    print(f"draw_graph median {med:.2f} ms")
    assert med < 25.0


def test_mock_v1_cases_render():
    """The v1 mockups (docs/ui/v1) stay renderable: same helpers as pages.py, every case."""
    from k1000.ui import mock_v1
    for name, page, s, status, focus, kw in mock_v1.demo_cases():
        img = mock_v1.render_v1(page, s, status, focus, **kw)
        assert img.size == (480, 320), name
