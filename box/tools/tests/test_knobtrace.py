# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for tools/k1000-knobtrace.py: the analysis on synthetic traces (no hardware).

The capture side needs the real MCP23017 with k1000.service stopped (docs/08-knob-precision.md);
here only its guards and plan are tested. Run from box/:

    python3 -m pytest -q -p no:cacheprovider tools/tests/test_knobtrace.py
"""
from __future__ import annotations

import collections
import importlib.util
import sys
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "k1000-knobtrace.py"


def _load():
    spec = importlib.util.spec_from_file_location("k1000_knobtrace", TOOL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["k1000_knobtrace"] = mod          # dataclasses resolve the module by name
    spec.loader.exec_module(mod)
    return mod


kt = _load()


def slow_session(spd: int, **click_kw) -> "kt.Synth":
    s = kt.Synth(spd=spd)
    s.guided("e1-cw", 0, 1, 10, body=lambda x: x.clicks(0, 1, 10, **click_kw))
    s.guided("e1-ccw", 0, -1, 10, body=lambda x: x.clicks(0, -1, 10, **click_kw))
    return s


def enc(res: dict, i: int = 0) -> dict:
    return res["encoders"][i]


# ------------------------------------------------------------------------------ trace file
def test_trace_file_round_trip(tmp_path):
    p = tmp_path / "t.trace"
    w = kt.TraceWriter(str(p), {"encoders": [list(e) for e in kt.ENC_PINS],
                                "switches": list(kt.SW_PINS), "labels": {"0": "E1 top-left"}}, 100.0)
    assert w.sample(100.0, 0xFFFF, "seed", force=True)
    assert not w.sample(100.001, 0xFFFF, "gpio")               # unchanged: not written
    assert w.sample(100.010, 0xFFFD, "cap", 0.0012)
    assert w.sample(100.005, 0xFFF9, "gpio")                   # never back in time
    w.directive("mark", {"t": 0.02, "phase": "start", "step": "x", "enc": 0})
    w.directive("stats", {"mode": "irq", "reads": 3})
    w.close()
    tr = kt.read_trace(p)
    assert [(round(t, 6), lv, src) for t, lv, src, _ in tr.samples] == [
        (0.0, 0xFFFF, "seed"), (0.01, 0xFFFD, "cap"), (0.01, 0xFFF9, "gpio")]
    assert tr.samples[1][3] == pytest.approx(0.0012)
    assert tr.marks[0]["step"] == "x" and tr.stats == [{"mode": "irq", "reads": 3}]
    assert tr.label(0) == "E1 top-left" and tr.label(1) == "enc1"
    with pytest.raises(ValueError):
        kt.read_trace(["not a trace"])


def test_ab_helpers():
    assert kt.ab_str(3) == "11" and kt.ab_str(2) == "10"
    assert kt.parse_ab("01") == 1 and kt.parse_ab(3) == 3
    with pytest.raises(ValueError):
        kt.parse_ab("12")


# ------------------------------------------------------------------------------- verdicts
def test_four_transition_encoder_verdict_and_config():
    res = kt.analyze(slow_session(4).trace())
    e = enc(res)
    assert e["transitions"] == 80 and e["invalid"] == 0 and e["bounces"] == 0
    assert e["verdict"]["spd"] == 4 and e["verdict"]["detent"] == "11"
    assert e["verdict"]["confidence"] == "high"
    assert [s["per_click"] for s in res["steps"]] == [4.0, 4.0]
    assert [s["direction_ok"] for s in res["steps"]] == [True, True]
    assert res["config"]["steps_per_detent"][0] == 4
    assert enc(res, 1)["transitions"] == 0
    text = kt.report(res)
    assert "VERDICT: steps_per_detent 4, detent AB 11" in text
    assert 'detent_ab: ["11", "11", "11", "11"]' in text


def test_two_transition_encoder_verdict():
    res = kt.analyze(slow_session(2).trace())
    e = enc(res)
    assert e["verdict"]["spd"] == 2 and e["verdict"]["confidence"] == "high"
    assert set(e["rests"]) == {"11", "00"}
    assert [s["per_click"] for s in res["steps"]] == [2.0, 2.0]
    assert res["config"]["steps_per_detent"] == [2, 4, 4, 4]


def test_verdict_from_rests_alone_without_guidance():
    s = kt.Synth(spd=2)
    s.wait(0.5).clicks(0, 1, 12).clicks(0, -1, 7)
    v = enc(kt.analyze(s.trace()))["verdict"]
    assert v["spd"] == 2 and v["confidence"] == "medium" and v["basis"] == "whole trace"
    s4 = kt.Synth(spd=4)
    s4.wait(0.5).clicks(0, 1, 12)
    assert enc(kt.analyze(s4.trace()))["verdict"]["spd"] == 4


def test_detent_off_both_open_is_reported():
    s = kt.Synth(spd=4, rest=[0b00, 3, 3, 3])
    s.guided("e1-cw", 0, 1, 10)
    v = enc(kt.analyze(s.trace()))["verdict"]
    assert v["spd"] == 4 and v["detent"] == "00"


def test_contact_bounce_is_counted_and_harmless():
    s = slow_session(4, bounce=2)
    res = kt.analyze(s.trace())
    e = enc(res)
    assert e["bounces"] >= 150                        # 2 per transition, 80 transitions
    assert e["verdict"]["spd"] == 4
    assert [s["per_click"] for s in res["steps"]] == [4.0, 4.0]
    v0 = res["steps"][0]["decoders"]["v0 decoder, spd 4 (2026-10-12)"]
    assert v0["emitted"] == 10 and v0["wrong_way"] == 0


def test_fast_spin_outrunning_1khz_polling_shows_skips():
    s = kt.Synth(spd=4)
    s.wait(0.5).spin(0, 1, 24, 60.0)                  # 240 transitions/s: 4.2 ms apart
    s.spin(0, 1, 24, 400.0)                           # 1600/s: 0.6 ms apart
    tr = kt.resample(s.trace(), 0.001)
    e = enc(kt.analyze(tr))
    assert e["invalid"] > 0                           # states skipped between two polls
    assert e["invalid_unattributed"] == 0             # ... all credited in the turn direction
    assert e["turn_dps_max"] >= 60
    perfect = enc(kt.analyze(s.trace()))
    assert perfect["invalid"] == 0 and perfect["gaps_under_i2c_read"] > 0


def test_resample_merges_fast_changes():
    s = kt.Synth(spd=4)
    s.wait(0.5).click(0, 1, dur=0.004)               # 4 transitions in 4 ms
    tr = kt.resample(s.trace(), 0.01)
    assert len(tr.samples) < len(s.trace().samples)


# ------------------------------------------------------------------- the v0 decoder's bug
def test_pause_at_the_top_of_each_click_doubles_the_v0_count():
    """docs/08: the v0 decoder emits after a 0.3 s pause off its anchor and re-anchors there."""
    s = kt.Synth(spd=4)
    s.guided("e1-fine", 0, 1, 5, "fine",
             body=lambda x: x.clicks(0, 1, 5, dur=0.3, hover_at=2, hover_s=0.5, pause=0.6))
    res = kt.analyze(s.trace())
    step = res["steps"][0]
    assert step["per_click"] == 4.0 and step["hovers"] == 5
    v0 = step["decoders"]["v0 decoder, spd 4 (2026-10-12)"]
    assert v0["emitted"] == 10 and v0["timer"] == 10
    assert enc(res)["hovers"] == 5


def test_v0_decoder_on_a_two_transition_encoder_depends_on_speed():
    s = kt.Synth(spd=2)
    s.guided("brisk", 0, 1, 10, body=lambda x: x.clicks(0, 1, 10, pause=0.1))
    s.guided("paused", 0, 1, 10, body=lambda x: x.clicks(0, 1, 10, pause=0.5))
    res = kt.analyze(s.trace())
    brisk, paused = (st["decoders"]["v0 decoder, spd 4 (2026-10-12)"] for st in res["steps"])
    assert brisk["emitted"] == 5                       # one step per two clicks
    assert paused["emitted"] == 10 and paused["timer"] >= 5   # one per click, half of them late


def test_fixed_decoder_is_replayed_when_the_driver_has_it():
    calls = []

    class Fixed:  # the shape of the proposed QuadratureDecoder (rest=, learn_rests=)
        def __init__(self, steps_per_detent=4, state=3, now=0.0, idle_s=0.3, infer_s=0.1,
                     rest=None, learn_rests=0):
            calls.append((steps_per_detent, rest, learn_rests))
            self.inner = kt.V0Decoder(steps_per_detent, state, now)
            self.inner.anchor = 3 if rest is None else rest

        def update(self, s, t):
            return self.inner.update(s, t)

        def deadline(self):
            return None

        def settle(self, now):
            return 0

    res = kt.analyze(slow_session(4).trace(), Fixed)
    names = set(res["steps"][0]["decoders"])
    assert "fixed decoder, spd 4 detent 11" in names and "fixed decoder, spd 2 detent 11" in names
    assert (4, 3, 3) in calls
    assert res["steps"][0]["decoders"]["fixed decoder, spd 4 detent 11"]["emitted"] == 10
    old = kt.analyze(slow_session(4).trace(), kt.V0Decoder)        # a v0 driver: no extra rows
    assert not any(n.startswith("fixed") for n in old["steps"][0]["decoders"])


# ------------------------------------------------------------------ guided bookkeeping
def test_redone_step_is_ignored_and_reversed_direction_flagged():
    s = kt.Synth(spd=4)
    s.wait(0.5).mark("start", "e1-cw", enc=0, dir=1, clicks=10, kind="slow")
    s.clicks(0, 1, 7).mark("end", "e1-cw", discard=True)          # miscounted: redone
    s.wait(0.5).mark("start", "e1-cw", enc=0, dir=1, clicks=10, kind="slow")
    s.clicks(0, -1, 10).mark("end", "e1-cw")                       # turned the other way
    res = kt.analyze(s.trace())
    assert len(res["steps"]) == 1
    st = res["steps"][0]
    assert st["net"] == -40 and st["per_click"] == 4.0 and st["direction_ok"] is False
    assert "DIRECTION REVERSED" in kt.report(res)


def test_click_rates_for_the_acceleration():
    s = kt.Synth(spd=4)
    s.wait(0.5).spin(0, 1, 20, 25.0)
    a = enc(kt.analyze(s.trace()))["accel"]
    assert a["intervals"] >= 18 and a["over_12"] == 1.0 and a["over_40"] == 0.0
    assert a["median_dps"] == pytest.approx(25.0, rel=0.05)


def test_detent_arrivals_ignore_a_bounce_back_into_the_same_detent():
    trs = kt.transitions(3, [(1.0, 1), (1.01, 3), (1.1, 1), (1.11, 0), (1.12, 2), (1.13, 3)])
    assert kt.detent_arrivals(3, trs, 4, 3) == [(1.13, 1)]


def test_switch_presses_and_chatter():
    s = kt.Synth(spd=4)
    s.wait(0.2).press(5).press(5, chatter=3)
    rep = kt.switch_report(s.trace())
    assert rep == [{"switch": 5, "pin": "GPA7", "presses": 5, "flips": 10, "chatter": 6}]


def test_guided_plan_covers_all_knobs_and_both_modes():
    full, quick = kt.guided_plan(), kt.guided_plan(quick=True)
    assert {s.enc for s in full} == {0, 1, 2, 3} and {s.enc for s in quick} == {0}
    assert {s.mode for s in full} == {"irq", "poll"}
    assert all(s.clicks for s in full if s.kind == "slow")
    assert len(full) <= 10                                        # about 2 minutes


def test_encoder_map_and_labels(tmp_path):
    y = tmp_path / "k1000.yaml"
    y.write_text("controls:\n  encoders: [1, 0, 2, 3]\n")
    m = kt.encoder_map(str(y))
    assert m == [1, 0, 2, 3]
    assert kt.encoder_labels(m)["1"] == "E1 top-left"
    y.write_text("controls:\n  encoders: [0, 0, 2, 3]\n")      # invalid -> default
    assert kt.encoder_map(str(y)) == [0, 1, 2, 3]
    assert kt.encoder_map(str(tmp_path / "missing.yaml")) == [0, 1, 2, 3]


# --------------------------------------------------------------------------------- CLI
def test_cli_analyze_a_saved_trace(tmp_path, capsys):
    s = slow_session(4)
    tr = s.trace()
    p = tmp_path / "s.trace"
    lines = [kt.MAGIC, "#meta " + __import__("json").dumps(tr.meta)]
    lines += [f"#mark {__import__('json').dumps(m)}" for m in tr.marks]
    lines += [f"{t:.6f} {lv:04x} {src} -" for t, lv, src, _ in tr.samples]
    p.write_text("\n".join(lines) + "\n")
    out_json = tmp_path / "a.json"
    assert kt.main(["analyze", str(p), "--json", str(out_json), "--no-replay"]) == 0
    out = capsys.readouterr().out
    assert "VERDICT: steps_per_detent 4" in out and "v0 decoder" in out
    assert __import__("json").loads(out_json.read_text())["config"]["steps_per_detent"][0] == 4


def test_cli_capture_refuses_while_k1000_runs(monkeypatch, capsys):
    monkeypatch.setattr(kt, "k1000_active", lambda: True)
    opened = []
    monkeypatch.setattr(kt, "Rig", lambda *a, **k: opened.append(1))
    assert kt.main(["capture", "--guided"]) == 3
    assert "sudo systemctl stop k1000" in capsys.readouterr().err
    assert opened == []                                           # the chip was never touched


def test_cli_help_and_demo(capsys):
    with pytest.raises(SystemExit) as e:
        kt.main(["--help"])
    assert e.value.code == 0
    assert "capture" in capsys.readouterr().out
    assert kt.main(["demo"]) == 0
    out = capsys.readouterr().out
    assert out.count("VERDICT") >= 2


# ======================================================================= bench / load (PERF-4)
def test_render_load_models_the_v1_service():
    """The load the capture and the bench put beside the knob worker: SHOW with a moving
    spectrum rendered in a thread, ballistics + save chunks in an asyncio loop thread."""
    load = kt.RenderLoad("render", save_ms=5.0, save_every_s=0.05)
    load.start()
    import time as _t
    _t.sleep(0.6)
    load.stop()
    assert not load.is_alive()
    assert load.kind == "render" and load.frames >= 3 and load.saves >= 3 and load.ms


def test_bench_drives_the_real_worker_on_the_sim_chip(tmp_path):
    out = tmp_path / "bench.json"
    prev = sys.getswitchinterval()
    try:
        assert kt.main(["bench", "--quick", "--save-ms", "10", "--json", str(out)]) == 0
    finally:
        sys.setswitchinterval(prev)
    import json as _json
    res = _json.loads(out.read_text())
    assert res["switch_ms"] == 1.0 and len(res["runs"]) == 2
    for r in res["runs"]:
        assert r["turned"] == 24 and r["lat_ms"] and r["storms"] == 0
    assert res["runs"][1]["load"] == "render" and res["runs"][1]["render_frames"] > 0
