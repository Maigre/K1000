# SPDX-License-Identifier: GPL-3.0-or-later
"""dspconfig + profiles: config generation, live diff ops, adoption, drift, click-safe plans.

Unit tests need no hardware. Tests marked `engine` run the real camilladsp binary (on the box):
`camilladsp -c` validity of generated configs, and a private instance on a SignalGenerator ->
File(/dev/null) config (never the ALSA card) driven over the v5 websocket.
Port: $K1000_DSPCONFIG_PORT (default 12341).
"""
import contextlib
import json
import math
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from k1000 import dspconfig as d
from k1000 import profiles as pf
from k1000.state import RANGES, State, delay_samples

REPO_BOX = Path(__file__).resolve().parents[1]
CDSP = shutil.which("camilladsp") or ("/usr/local/bin/camilladsp"
                                      if os.path.exists("/usr/local/bin/camilladsp") else None)
PORT = int(os.environ.get("K1000_DSPCONFIG_PORT", "12341"))


def dev():
    return pf.load_profile("dev", probe=False)


def m4(**ov):
    return pf.load_profile("m4", ov or None, probe=False)


def extreme_state() -> State:
    s = State(master_db=0.0, balance_db=12.0, input_trim_db=12.0, sub_delay_ms=50.0,
              top_delay_ms=50.0, lowcut_hz=300.0, sub_lowpass_on=True, sub_lowpass_hz=40.0,
              sub_inverted=True, top_inverted=True, out_trim_db=[6.0, -24.0, 6.0, -24.0])
    for b in s.sys_peq:
        b.freq, b.gain, b.q = 20000.0, 10.0, 16.0
    for b in s.live_peq:
        b.freq, b.gain, b.q = 20.0, -20.0, 0.3
    s.live_lowshelf.gain, s.live_highshelf.gain = 6.0, -12.0
    return s


def resolve(cfg, ptr):
    node = cfg
    for part in d._unpointer(ptr):
        node = node[part]
    return node


# ============================================================================ profiles
def test_dev_profile_matches_contract():
    p = dev()
    assert (p.capture_device, p.playback_device) == ("hw:sndrpihifiberry", "hw:sndrpihifiberry")
    assert (p.capture_channels, p.playback_channels) == (2, 2)
    assert (p.capture_format, p.playback_format) == ("S32_LE", "S32_LE")
    assert p.input_map == (0, 1) and p.audition and not p.pinned and not p.probe


def test_m4_profile_defaults():
    p = m4()
    assert p.capture_device == "hw:M4" and p.playback_channels == 4
    assert p.capture_channels == 4                         # assumption until probed
    assert p.input_map == (2, 3)                           # line inputs 3-4
    assert p.output_map == (2, 3, 0, 1)                    # out1 TopL, out2 TopR, out3 SubL, out4 SubR
    assert p.pinned and not p.audition
    assert p.capture_format is None                        # CamillaDSP auto-selects


def test_unknown_profile():
    with pytest.raises(pf.ProfileError):
        pf.load_profile("usb")


def test_overrides_from_settings_file_and_names():
    settings = {"profiles": {"m4": {"input_map": [0, 1], "output_map": ["SubL", "SubR", "TopL", "TopR"],
                                    "capture_channels": 6}}}
    p = pf.load_profile("m4", settings, probe=False)
    assert p.input_map == (0, 1) and p.output_map == (0, 1, 2, 3) and p.capture_channels == 6
    p2 = pf.load_profile("m4", {"output_map": [3, 2, 1, 0]}, probe=False)   # section form
    assert p2.output_map == (3, 2, 1, 0)


def test_bad_maps_fall_back_with_warnings():
    p = pf.load_profile("m4", {"capture_channels": 2, "output_map": ["woofer"]}, probe=False)
    assert p.input_map == (0, 1)                          # (2, 3) does not exist on 2 channels
    assert p.output_map == (2, 3, 0, 1)
    assert any("input_map" in w for w in p.warnings) and any("woofer" in w for w in p.warnings)
    p = pf.load_profile("m4", {"output_map": ["TopL", "TopR"]}, probe=False)
    assert any("not routed" in w for w in p.warnings)


def test_bad_input_map_falls_back_to_the_profiles_own_inputs():
    """m4 'input_map: [3, 4]' (reading 'line inputs 3-4' literally) must not feed the PA from
    inputs 1-2 (the mic preamps): the profile's own line inputs are used, with a warning."""
    p = pf.load_profile("m4", {"profiles": {"m4": {"input_map": [3, 4]}}}, probe=False)
    assert p.input_map == (2, 3) and any("input_map" in w for w in p.warnings)


def test_target_level_per_profile():
    assert pf.load_profile("m4", {}, probe=False).target_level == 256
    p = pf.load_profile("m4", {"profiles": {"m4": {"target_level": 768}}}, probe=False)
    assert p.target_level == 768 and not p.warnings
    assert d.generate(p, State())["devices"]["target_level"] == 768
    assert d.generate(pf.load_profile("dev", {}), State())["devices"]["target_level"] == 256
    for bad, want in ((5000, 1536), (10, 64), ("768", 256), (512.5, 256), (True, 256)):
        q = pf.load_profile("m4", {"profiles": {"m4": {"target_level": bad}}}, probe=False)
        assert q.target_level == want and any("target_level" in w for w in q.warnings), bad


def _fake_prober(probe):
    return lambda device: probe


def test_m4_adapts_to_probe():
    hw_c = pf.HwParams(6, 6, ["S24_3_LE"], ["S24_3LE"], 44100, 192000)
    hw_p = pf.HwParams(4, 4, ["S32_LE", "S24_3_LE"], ["S32_LE", "S24_3LE"], 44100, 192000)
    probe = pf.AlsaProbe("hw:M4", present=True, playback=hw_p, capture=hw_c, source="dump-hw-params")
    p = pf.load_profile("m4", {}, prober=_fake_prober(probe))
    assert p.present is True and p.probe
    assert p.capture_channels == 6 and p.playback_channels == 4      # counts: the probe decides
    # formats: never from the probe (devices block must not depend on whether it ran)
    assert p.capture_format is None and p.playback_format is None
    assert p.input_map == (2, 3) and not p.warnings


def test_probe_beats_refused_explicit_value():
    hw = pf.HwParams(4, 4, ["S32_LE"], ["S32_LE"])
    probe = pf.AlsaProbe("hw:M4", present=True, playback=hw, capture=hw)
    p = pf.load_profile("m4", {"capture_channels": 2, "capture_format": "S16_LE"},
                        prober=_fake_prober(probe))
    assert p.capture_channels == 4 and p.capture_format is None     # refused -> auto-select
    assert len(p.warnings) == 2
    ok = pf.load_profile("m4", {"capture_format": "S32_LE"}, prober=_fake_prober(probe))
    assert ok.capture_format == "S32_LE" and not ok.warnings          # explicit + offered: kept


def test_m4_missing_is_reported_not_replaced():
    probe = pf.AlsaProbe("hw:M4", present=False, error="hw:M4: card not present")
    p = pf.load_profile("m4", {}, prober=_fake_prober(probe))
    assert p.present is False and p.capture_device == "hw:M4"      # no silent fallback
    assert any("not present" in w for w in p.warnings)


DOC_DUMP = """HW Params of device "hw:Generic":
--------------------
ACCESS:  MMAP_INTERLEAVED RW_INTERLEAVED
FORMAT:  S16_LE S32_LE
SUBFORMAT:  STD
SAMPLE_BITS: [16 32]
CHANNELS: 2
RATE: [44100 48000]
PERIOD_TIME: (333 96870749)
--------------------
aplay: set_params:1343: Sample format non available
"""

USB_STREAM = """MOTU M4 at usb-0000:01:00.0-1.3, high speed : USB Audio

Playback:
  Status: Stop
  Interface 1
    Altset 1
    Format: S32_LE
    Channels: 4
    Endpoint: 0x01 (1 OUT) (ASYNC)
    Rates: 44100, 48000, 88200, 96000, 176400, 192000
    Data packet interval: 125 us
    Bits: 24

Capture:
  Status: Stop
  Interface 2
    Altset 1
    Format: S32_LE
    Channels: 4
    Rates: 44100, 48000, 96000
"""


def test_parse_hw_params():
    hw = pf.parse_hw_params(DOC_DUMP)
    assert hw.channels_min == hw.channels_max == 2
    assert hw.formats == ["S16_LE", "S32_LE"] and (hw.rate_min, hw.rate_max) == (44100, 48000)
    hw = pf.parse_hw_params("CHANNELS: [1 8]\nFORMAT:  S24_3LE S24_LE FLOAT_LE U8\n")
    assert (hw.channels_min, hw.channels_max) == (1, 8)
    assert hw.formats == ["S24_3_LE", "S24_4_LE", "F32_LE"]
    assert pf.parse_hw_params("aplay: main:850: audio open error: Device or resource busy") is None


def test_parse_usb_stream():
    r = pf.parse_usb_stream(USB_STREAM)
    assert r["playback"].channels_max == 4 and r["capture"].channels_max == 4
    assert r["capture"].formats == ["S32_LE"] and r["capture"].rate_max == 96000
    assert pf.parse_usb_stream("") == {}


def test_best_format_and_card_id(tmp_path):
    assert pf.best_format(["S16_LE", "S24_3_LE", "S32_LE"]) == "S32_LE"
    assert pf.best_format(["F32_LE", "S16_LE"]) == "S16_LE"
    assert pf.best_format([]) is None
    assert pf.card_id("hw:M4") == "M4"
    assert pf.card_id("hw:CARD=M4,DEV=0") == "M4"
    assert pf.card_id("plughw:sndrpihifiberry") == "sndrpihifiberry"
    assert pf.card_id("hw:1,0") == "card1"
    assert pf.card_id("null") is None
    (tmp_path / "M4").mkdir()
    assert pf.card_present("hw:M4", tmp_path) is True
    assert pf.card_present("hw:Other", tmp_path) is False
    assert pf.card_present("null", tmp_path) is None


def test_probe_missing_card_never_runs_aplay(tmp_path, monkeypatch):
    def no_run(*a, **k):
        raise AssertionError("must not open a missing device")
    monkeypatch.setattr(pf.subprocess, "run", no_run)
    pr = pf.probe_alsa("hw:M4", proc_root=tmp_path)
    assert pr.present is False and "not present" in pr.error


def test_probe_busy_falls_back_to_usb_stream(tmp_path, monkeypatch):
    (tmp_path / "M4").mkdir()
    (tmp_path / "M4" / "stream0").write_text(USB_STREAM)
    monkeypatch.setattr(pf, "_dump", lambda tool, dev, t: (
        "aplay: main:850: audio open error: Device or resource busy", 1))
    pr = pf.probe_alsa("hw:M4", proc_root=tmp_path)
    assert pr.busy and pr.present and pr.source == "proc-stream"
    assert pr.capture.channels_max == 4 and pr.playback.formats == ["S32_LE"]


def test_settings_loading(tmp_path):
    assert pf.load_settings(tmp_path / "none.yaml") == {}          # missing: defaults, no error
    bad = tmp_path / "bad.yaml"
    bad.write_text("profile: [unclosed\n")
    got = pf.load_settings(bad)
    assert set(got) == {pf.SETTINGS_ERROR} and "does not parse" in got[pf.SETTINGS_ERROR]
    bad.write_text("- a list\n")
    assert set(pf.load_settings(bad)) == {pf.SETTINGS_ERROR}
    bad.write_text("")
    assert set(pf.load_settings(bad)) == {pf.SETTINGS_ERROR}       # empty file: flagged
    assert pf.select_profile_name({"profile": "m4"}, "dev") == "m4"
    assert pf.select_profile_name({"profile": "bogus"}, "m4") == "m4"
    assert pf.select_profile_name({}, None) == "dev"


def test_shipped_settings_file_loads():
    settings = pf.load_settings(REPO_BOX / "etc" / "k1000.yaml")
    assert settings["profile"] in pf.PROFILE_NAMES
    for name in pf.PROFILE_NAMES:
        p = pf.load_profile(name, settings, probe=False)
        assert not p.warnings, p.warnings
    assert pf.load_profile("m4", settings, probe=False).output_map == (2, 3, 0, 1)
    assert pf.load_profile("dev", settings).probe is False         # the shared HiFiBerry is never probed


# ============================================================================ generate
CONTRACT_FILTERS = (
    {"in_trim", "live_lowshelf", "live_highshelf", "sub_trim", "sub_lowpass", "sub_delay",
     "sub_fader", "top_trim", "top_lowcut", "top_delay", "top_fader"}
    | {f"sys_peq_{i}" for i in range(1, 9)} | {f"live_peq_{i}" for i in range(1, 5)}
    | {f"out_trim_{i}" for i in range(4)}
)


def test_generate_structure_matches_contract():
    cfg = d.generate(dev(), State())
    dv = cfg["devices"]
    assert (dv["samplerate"], dv["chunksize"], dv["target_level"]) == (48000, 256, 256)
    assert dv["volume_ramp_time_ms"] == 100.0 and dv["enable_rate_adjust"] is False
    assert dv["volume_limit"] == RANGES["master_db"].max
    assert dv["capture"] == {"type": "Alsa", "channels": 2, "device": "hw:sndrpihifiberry", "format": "S32_LE"}
    assert set(cfg["filters"]) == CONTRACT_FILTERS
    assert set(cfg["mixers"]) == {"input", "split", "output"}
    steps = [(s["type"], s.get("name"), tuple(s.get("names", ())), tuple(s.get("channels") or ()))
             for s in cfg["pipeline"]]
    assert steps[0] == ("Mixer", "input", (), ())
    assert steps[1][2][0] == "in_trim" and steps[1][3] == (0, 1)
    assert steps[1][2][1:] == tuple([f"sys_peq_{i}" for i in range(1, 9)] + [f"live_peq_{i}" for i in range(1, 5)]
                                    + ["live_lowshelf", "live_highshelf"])
    assert steps[2] == ("Mixer", "split", (), ())
    assert steps[3][2:] == (("sub_trim", "sub_lowpass", "sub_delay", "sub_fader"), (0, 1))
    assert steps[4][2:] == (("top_trim", "top_lowcut", "top_delay", "top_fader"), (2, 3))
    assert [s[2:] for s in steps[5:9]] == [((f"out_trim_{i}",), (i,)) for i in range(4)]
    assert steps[9] == ("Mixer", "output", (), ())


def test_fader_ramps_are_100ms_and_an_old_300ms_engine_is_fixed_safely():
    """D-009 / docs/08 row 3: a 300 ms ramp, restarted on every detent, made master and balance
    trail the hand. 100 ms on Main (devices) and both Aux Volume filters; the guard dip follows.
    An engine still running a 300 ms config (before a deploy): the Aux ramps are plain value
    fixes (no restart, no rebuild); Main's is a devices drift, never fixed while audio runs."""
    cfg = d.generate(dev(), State())
    assert d.VOLUME_RAMP_MS == 100.0
    assert cfg["devices"]["volume_ramp_time_ms"] == 100.0
    assert [cfg["filters"][n]["parameters"]["ramp_time_ms"] for n in ("sub_fader", "top_fader")] \
        == [100.0, 100.0]
    assert d.GUARD_WAIT_S == pytest.approx(0.15)
    old = json.loads(json.dumps(cfg))
    old["devices"]["volume_ramp_time_ms"] = 300.0
    for n in ("sub_fader", "top_fader"):
        old["filters"][n]["parameters"]["ramp_time_ms"] = 300.0
    drift = d.config_drift(old, cfg)
    assert drift.devices == ["devices.volume_ramp_time_ms 300.0 != 100.0"] and drift.rebuild == []
    assert set(drift.fixes) == {d.SetValue(f"/filters/{n}/parameters/ramp_time_ms", 100.0)
                                for n in ("sub_fader", "top_fader")}


def test_filter_types_and_off_values():
    f = d.generate(dev(), State())["filters"]
    assert f["sys_peq_1"]["parameters"]["type"] == "Peaking"
    assert f["live_lowshelf"]["parameters"]["type"] == "Lowshelf"
    assert f["live_highshelf"]["parameters"]["type"] == "Highshelf"
    assert f["top_lowcut"]["parameters"] == {"type": "Highpass", "freq": 100.0, "q": 0.707}
    assert f["sub_lowpass"]["parameters"] == {"type": "Lowpass", "freq": 20000.0, "q": 0.707}   # off
    assert f["sub_delay"]["parameters"] == {"delay": 0.0, "delay_unit": "ms", "subsample": False}
    assert f["sub_fader"]["parameters"]["fader"] == "Aux1" and f["top_fader"]["parameters"]["fader"] == "Aux2"
    assert f["sub_fader"]["parameters"]["limit"] == RANGES["balance_db"].max / 2
    s = State(lowcut_on=False, sub_lowpass_on=True, sub_lowpass_hz=90.0, top_inverted=True)
    f = d.generate(dev(), s)["filters"]
    assert f["top_lowcut"]["parameters"]["freq"] == 10.0
    assert f["sub_lowpass"]["parameters"]["freq"] == 90.0
    assert f["top_trim"]["parameters"]["inverted"] is True and f["sub_trim"]["parameters"]["inverted"] is False


def test_split_mixer_mono_and_stereo():
    st = d.generate(dev(), State())["mixers"]["split"]["mapping"]
    assert [[(x["channel"], x["gain"]) for x in m["sources"]] for m in st] == [[(0, 0.0)], [(1, 0.0)], [(0, 0.0)], [(1, 0.0)]]
    mo = d.generate(dev(), State(mono_subs=True))["mixers"]["split"]["mapping"]
    assert [(x["channel"], x["gain"]) for x in mo[0]["sources"]] == [(0, -6.0), (1, -6.0)]
    assert [(x["channel"], x["gain"]) for x in mo[1]["sources"]] == [(0, -6.0), (1, -6.0)]
    assert [(x["channel"], x["gain"]) for x in mo[2]["sources"]] == [(0, 0.0)]


@pytest.mark.parametrize("aud,expect", [
    ("sum", [[(0, -6.0), (2, -6.0)], [(1, -6.0), (3, -6.0)]]),
    ("tops", [[(2, 0.0)], [(3, 0.0)]]),
    ("subs", [[(0, 0.0)], [(1, 0.0)]]),
])
def test_dev_audition_output_mixer(aud, expect):
    om = d.generate(dev(), State(dev_audition=aud))["mixers"]["output"]
    assert om["channels"] == {"in": 4, "out": 2}
    assert [[(x["channel"], x["gain"]) for x in m["sources"]] for m in om["mapping"]] == expect


def test_m4_output_and_input_mixers():
    cfg = d.generate(m4(), State(profile="m4"))
    om = cfg["mixers"]["output"]
    assert om["channels"] == {"in": 4, "out": 4}
    assert [(m["dest"], m["sources"][0]["channel"]) for m in om["mapping"]] == [(0, 2), (1, 3), (2, 0), (3, 1)]
    im = cfg["mixers"]["input"]
    assert im["channels"] == {"in": 4, "out": 2}
    assert [m["sources"][0]["channel"] for m in im["mapping"]] == [2, 3]
    assert "format" not in cfg["devices"]["capture"]          # unprobed: let CamillaDSP choose


def test_generate_clamps_unvalidated_state():
    s = State()
    s.master_db, s.input_trim_db, s.sub_delay_ms = 30.0, 99.0, -4.0
    s.live_peq[0].gain, s.live_peq[0].freq, s.live_peq[0].q = 40.0, 30000.0, 0.0
    s.out_trim_db[2] = float("nan")
    f = d.generate(dev(), s)["filters"]
    assert f["in_trim"]["parameters"]["gain"] == 12.0
    assert f["sub_delay"]["parameters"]["delay"] == 0.0
    assert f["live_peq_1"]["parameters"] == {"type": "Peaking", "freq": 20000.0, "gain": 6.0, "q": 0.3}
    assert f["out_trim_2"]["parameters"]["gain"] == 0.0
    assert d.fader_levels(s)[d.FADER_MAIN] == 0.0


def test_to_yaml_roundtrip():
    cfg = d.generate(m4(), extreme_state())
    text = d.to_yaml(cfg)
    assert text.startswith("# Generated by k1000")
    assert yaml.safe_load(text) == cfg


@pytest.mark.skipif(not getattr(yaml, "__with_libyaml__", False), reason="no libyaml")
def test_libyaml_writes_active_yml_byte_for_byte_like_the_python_dumper():
    """PERF-3: active.yml is dumped with libyaml (CSafeDumper, ~4x faster on the Pi): the text
    must be exactly the pure-Python SafeDumper's (the engine, the .prev copy and the drift
    checks read it; a test or a diff must not see a change)."""
    assert d.YAML_DUMPER is yaml.CSafeDumper
    states = [State(), extreme_state(), State(mono_subs=True, sub_inverted=True, top_inverted=True,
                                              lowcut_on=False, dev_audition="tops")]
    for name in pf.PROFILE_NAMES:
        prof = pf.load_profile(name, {}, probe=False)
        for s in states:
            for cfg in (d.generate(prof, s), d.signal_generator_variant(d.generate(prof, s))):
                assert d.to_yaml(cfg, yaml.SafeDumper) == d.to_yaml(cfg), (name, s)


def test_signal_generator_variant_keeps_channels():
    cfg = d.generate(m4(capture_channels=6), State())
    v = d.signal_generator_variant(cfg)
    assert v["devices"]["capture"]["type"] == "SignalGenerator" and v["devices"]["capture"]["channels"] == 6
    assert v["devices"]["playback"] == {"type": "File", "channels": 4, "filename": "/dev/null", "format": "S32_LE"}
    assert cfg["devices"]["capture"]["type"] == "Alsa"         # original untouched


# ============================================================================ diff ops
def test_no_change_no_ops():
    assert d.diff_ops(State(), State()) == []
    assert d.diff_ops(extreme_state(), extreme_state()) == []


def test_param_ops_use_contract_pointers():
    a = State()
    b = a.copy()
    b.live_peq[0].gain = -6.0
    b.input_trim_db = 2.0
    b.sub_delay_ms = 1.25
    b.top_inverted = True
    b.out_trim_db[3] = -1.5
    b.live_highshelf.freq = 6300.0
    ops = d.diff_ops(a, b)
    assert set(ops) == {
        d.SetValue("/filters/live_peq_1/parameters/gain", -6.0),
        d.SetValue("/filters/in_trim/parameters/gain", 2.0),
        d.SetValue("/filters/sub_delay/parameters/delay", 1.25),
        d.SetValue("/filters/top_trim/parameters/inverted", True),
        d.SetValue("/filters/out_trim_3/parameters/gain", -1.5),
        d.SetValue("/filters/live_highshelf/parameters/freq", 6300.0),
    }


def test_every_owned_pointer_resolves_in_generated_config():
    for prof, s in ((dev(), State()), (m4(), extreme_state())):
        cfg = d.generate(prof, s)
        for ptr, val in d.param_map(s).items():
            assert resolve(cfg, ptr) == val, ptr


def test_lowcut_toggle_and_hidden_changes():
    a = State()
    assert d.diff_ops(a, State(lowcut_on=False)) == [d.SetValue("/filters/top_lowcut/parameters/freq", 10.0)]
    off = State(lowcut_on=False)
    assert d.diff_ops(off, State(lowcut_on=False, lowcut_hz=150.0)) == []     # off: freq parked
    assert d.diff_ops(off, State(lowcut_on=True, lowcut_hz=150.0)) == [
        d.SetValue("/filters/top_lowcut/parameters/freq", 150.0)]
    assert d.diff_ops(a, State(sub_lowpass_on=True)) == [
        d.SetValue("/filters/sub_lowpass/parameters/freq", 120.0)]


def test_fader_ops_balance_master_mute():
    ops = d.diff_ops(State(), State(balance_db=5.0))
    assert ops == [d.Fader(1, 2.5), d.Fader(2, -2.5)]
    assert d.diff_ops(State(), State(master_db=-10.0)) == [d.Fader(0, -10.0)]
    assert d.diff_ops(State(), State(muted=True)) == [d.FaderMute(0, True)]
    assert d.diff_ops(State(), State(balance_db=40.0)) == [d.Fader(1, 6.0), d.Fader(2, -6.0)]   # clamped
    assert d.fader_ops(State(balance_db=-3.0, master_db=-15.0, muted=True)) == [
        d.Fader(1, -1.5), d.Fader(2, 1.5), d.FaderMute(1, False), d.FaderMute(2, False),
        d.Fader(0, -15.0), d.FaderMute(0, True)]


def test_rebuild_ops():
    ops = d.diff_ops(State(), State(mono_subs=True, master_db=-30.0, input_trim_db=3.0))
    assert isinstance(ops[0], d.Rebuild) and "mono_subs" in ops[0].reason
    assert not any(isinstance(o, d.SetValue) for o in ops)      # carried by the new config
    assert d.Fader(0, -30.0) in ops                                # faders live outside the config
    assert isinstance(d.diff_ops(State(), State(profile="m4"))[0], d.Rebuild)
    assert isinstance(d.diff_ops(State(), State(dev_audition="tops"))[0], d.Rebuild)
    assert d.diff_ops(State(profile="m4"), State(profile="m4", dev_audition="tops")) == []


def test_op_to_command_and_patch():
    assert d.op_to_command(d.SetValue("/filters/x/parameters/gain", 1.0)) == {
        "command": "SetConfigValue", "pointer": "/filters/x/parameters/gain", "value": 1.0}
    assert d.op_to_command(d.Fader(1, -3.0)) == {"command": "SetFaderVolume", "fader": 1, "value": -3.0}
    assert d.op_to_command(d.FaderMute(0, True)) == {"command": "SetFaderMute", "fader": 0, "value": True}
    assert d.op_to_command(d.Rebuild("x")) is None
    patch = d.ops_to_patch([d.SetValue("/filters/a/parameters/gain", 1.0),
                            d.SetValue("/filters/a/parameters/freq", 100.0),
                            d.SetValue("/filters/b/parameters/inverted", True), d.Fader(0, -1.0)])
    assert patch == {"filters": {"a": {"parameters": {"gain": 1.0, "freq": 100.0}},
                                 "b": {"parameters": {"inverted": True}}}}
    assert d.pointer("filters", "a/b", "x~y") == "/filters/a~1b/x~0y"
    assert d._unpointer("/filters/a~1b/x~0y") == ["filters", "a/b", "x~y"]


# ============================================================================ adoption
def test_state_from_config_roundtrip():
    for prof, s in ((dev(), State()), (m4(), extreme_state()), (dev(), State(mono_subs=True, dev_audition="subs"))):
        cfg = d.generate(prof, s)
        faders = [{"volume": v, "mute": False} for v in (s.master_db, s.balance_db / 2, -s.balance_db / 2, 0, 0)]
        faders[0]["mute"] = s.muted
        assert d.state_from_config(json.dumps(cfg), faders, State()) == s.clamped()
        assert d.state_from_config(cfg, None, s) == s.clamped()


def test_adopts_external_edits_in_any_form():
    base = State()
    cfg = d.generate(dev(), base)
    f = cfg["filters"]
    f["live_peq_2"]["parameters"] = {"type": "Peaking", "freq": 1234.0, "gain": -4.0, "bandwidth": 1.0}
    f["sub_delay"]["parameters"] = {"delay": 96, "delay_unit": "samples", "subsample": False}
    f["top_delay"]["parameters"] = {"delay": 686.0, "delay_unit": "mm", "subsample": False}
    f["in_trim"]["parameters"].update(gain=0.5, scale="linear")
    f["top_lowcut"]["parameters"]["freq"] = 10.0
    f["sub_lowpass"]["parameters"]["freq"] = 80.0
    f["out_trim_1"]["parameters"]["gain"] = -99.0
    f["sys_peq_8"]["parameters"]["gain"] = "garbage"
    cfg["mixers"]["split"] = d._split_mixer(True)
    faders = [{"volume": -12.0, "mute": True}, {"volume": 2.0, "mute": False}, {"volume": -2.0, "mute": False}]
    s = d.state_from_config(cfg, faders, base)
    assert s.live_peq[1].freq == 1234.0 and s.live_peq[1].gain == -4.0
    assert math.isclose(s.live_peq[1].q, math.sqrt(2) / (2 - 1), rel_tol=1e-3)     # 1 octave = Q 1.414
    assert s.sub_delay_ms == 2.0 and s.top_delay_ms == 2.0
    assert math.isclose(s.input_trim_db, -6.02, abs_tol=0.01)
    assert s.lowcut_on is False and s.lowcut_hz == base.lowcut_hz       # off keeps the remembered freq
    assert s.sub_lowpass_on is True and s.sub_lowpass_hz == 80.0
    assert s.out_trim_db[1] == -24.0                                     # clamped
    assert s.sys_peq[7].gain == base.sys_peq[7].gain                     # garbage ignored
    assert s.mono_subs is True
    assert s.master_db == -12.0 and s.muted is True and s.balance_db == 4.0


def test_adoption_tolerates_garbage():
    base = extreme_state()
    for junk in ("", "{not json", "[]", b"null", {"filters": "x"}, {"filters": {"in_trim": 3}}, None, 42):
        assert d.state_from_config(junk, "nonsense", base) == base.clamped()


def test_adoption_skips_recent_writes():
    base = State()
    cfg = d.generate(dev(), base)
    cfg["filters"]["live_peq_1"]["parameters"]["gain"] = -9.0     # stale engine value
    cfg["filters"]["live_peq_2"]["parameters"]["gain"] = -3.0     # genuine GUI edit
    faders = [{"volume": -50.0, "mute": False}, {"volume": 1.0, "mute": False}, {"volume": -1.0, "mute": False}]
    s = d.state_from_config(cfg, faders, base, skip_pointers=["/filters/live_peq_1/parameters/gain"],
                            skip_faders=[0])
    assert s.live_peq[0].gain == 0.0 and s.live_peq[1].gain == -3.0
    assert s.master_db == base.master_db and s.balance_db == 2.0
    assert cfg["filters"]["live_peq_1"]["parameters"]["gain"] == -9.0   # caller's dict untouched


# ============================================================================ drift
def test_config_drift_clean_for_own_config():
    for prof, s in ((dev(), State()), (m4(), extreme_state())):
        cfg = d.generate(prof, s)
        drift = d.config_drift(json.dumps(cfg), cfg)
        assert drift == ([], [], []) and drift.clean


def test_config_drift_detects_structure_and_values():
    exp = d.generate(dev(), State())
    live = json.loads(json.dumps(exp))
    live["filters"]["top_lowcut"]["parameters"]["q"] = 0.5
    live["filters"]["live_peq_1"]["parameters"]["gain"] = -3.0
    devices, reasons, fixes = d.config_drift(live, exp)
    assert devices == [] and reasons == []
    assert set(fixes) == {d.SetValue("/filters/top_lowcut/parameters/q", 0.707),
                          d.SetValue("/filters/live_peq_1/parameters/gain", 0.0)}

    live = json.loads(json.dumps(exp))
    live["mixers"]["split"] = d._split_mixer(True)
    del live["filters"]["out_trim_3"]
    p = live["filters"]["live_peq_2"]["parameters"]
    p["bandwidth"] = p.pop("q")
    live["pipeline"][3]["names"].remove("sub_delay")
    live["devices"]["playback"]["device"] = "hw:Other"
    drift = d.config_drift(live, exp)
    assert drift.fixes == []
    text = " | ".join(drift.rebuild)
    for needle in ("mixer split", "out_trim_3 missing", "live_peq_2.q missing", "pipeline"):
        assert needle in text, needle
    assert drift.devices == ["devices.playback.device 'hw:Other' != 'hw:sndrpihifiberry'"]


# ============================================================================ click-safe plans
def _flatten(plan):
    return [op for _w, batch in plan for op in batch]


def test_smooth_lowcut_toggle_is_detent_sized():
    plan = d.smooth_ops(State(), State(lowcut_on=False))
    freqs = [op.value for op in _flatten(plan)]
    assert freqs[-1] == 10.0 and len(freqs) == math.ceil(math.log2(100 / 10) * 12)
    prev = 100.0
    for f in freqs:
        assert abs(math.log2(f / prev)) <= 1 / 12 + 1e-3
        prev = f
    assert all(w == d.STEP_WAIT_S for w, _ in plan[1:]) and plan[0][0] == 0.0


def test_smooth_reset_live_moves_all_bands_together():
    a = State()
    for b in a.live_peq:
        b.gain = -12.0
    plan = d.smooth_ops(a, a.live_reset())
    assert len(plan) == 24                                      # 12 dB / 0.5 dB
    assert all(len(batch) == 4 for _w, batch in plan)
    assert [op.value for op in plan[-1][1]] == [0.0] * 4


def test_smooth_trim_uses_fine_steps():
    plan = d.smooth_ops(State(), State(out_trim_db=[-6.0, 0, 0, 0]))
    assert len(plan) == 60 and plan[-1][1] == [d.SetValue("/filters/out_trim_0/parameters/gain", -6.0)]


def test_smooth_guards_delay_and_polarity_with_group_mute():
    plan = d.smooth_ops(State(), State(sub_delay_ms=2.5, top_inverted=True))
    assert plan[0] == (0.0, [d.FaderMute(1, True), d.FaderMute(2, True)])
    assert plan[1][0] == d.GUARD_WAIT_S and set(plan[1][1]) == {
        d.SetValue("/filters/sub_delay/parameters/delay", 2.5),
        d.SetValue("/filters/top_trim/parameters/inverted", True)}
    assert plan[2][1] == [d.FaderMute(1, False), d.FaderMute(2, False)]


def test_smooth_passes_rebuild_and_faders():
    assert d.smooth_ops(State(), State()) == []
    plan = d.smooth_ops(State(), State(mono_subs=True))
    assert len(plan) == 1 and isinstance(plan[0][1][0], d.Rebuild)
    plan = d.smooth_ops(State(), State(master_db=-10.0, live_peq=State().live_peq))
    assert plan == [(0.0, [d.Fader(0, -10.0)])]


def test_smooth_caps_steps():
    plan = d.smooth_ops(State(), State(input_trim_db=12.0), max_steps=10)
    assert len(plan) == 10 and plan[-1][1][0].value == 12.0


# ============================================================================ engine (on the box)
engine = pytest.mark.engine
needs_cdsp = pytest.mark.skipif(CDSP is None, reason="camilladsp binary not installed")


def _states():
    return {
        "dev_default": (dev(), State()),
        "dev_extreme": (dev(), extreme_state()),
        "dev_mono_tops": (dev(), State(mono_subs=True, dev_audition="tops", lowcut_on=False)),
        "dev_subs": (dev(), State(dev_audition="subs", sub_lowpass_on=True)),
        "m4_default_4ch": (m4(), State(profile="m4")),
        "m4_extreme": (m4(), extreme_state()),
        "m4_mono": (m4(), State(profile="m4", mono_subs=True)),
        "m4_6ch_s24": (m4(capture_channels=6, capture_format="S24_3_LE", playback_format="S32_LE"), State()),
    }


@engine
@needs_cdsp
@pytest.mark.parametrize("name", list(_states()))
def test_camilladsp_accepts_generated_config(tmp_path, name):
    prof, s = _states()[name]
    path = tmp_path / f"{name}.yml"
    path.write_text(d.to_yaml(d.generate(prof, s)))
    r = subprocess.run([CDSP, "-c", str(path)], capture_output=True, text=True, timeout=20)
    assert "Config is valid" in r.stdout + r.stderr, r.stdout + r.stderr
    assert r.returncode == 0


@engine
@needs_cdsp
def test_camilladsp_rejects_bad_config(tmp_path):
    """Negative control: `-c` really validates (so the positive test means something)."""
    cfg = d.generate(dev(), State())
    cfg["filters"]["top_lowcut"]["parameters"]["freq"] = 30000.0
    path = tmp_path / "bad.yml"
    path.write_text(d.to_yaml(cfg))
    r = subprocess.run([CDSP, "-c", str(path)], capture_output=True, text=True, timeout=20)
    assert "Config is valid" not in r.stdout + r.stderr


def _port_free(port):
    with socket.socket() as sk:
        return sk.connect_ex(("127.0.0.1", port)) != 0


@pytest.fixture
def engine_ws(tmp_path):
    ws_sync = pytest.importorskip("websockets.sync.client")
    if CDSP is None:
        pytest.skip("camilladsp binary not installed")
    if not _port_free(PORT):
        pytest.skip(f"port {PORT} busy")
    cfg = d.signal_generator_variant(d.generate(dev(), State()))
    path = tmp_path / "engine.yml"
    path.write_text(d.to_yaml(cfg))
    log = tmp_path / "engine.log"
    proc = subprocess.Popen([CDSP, "-v", "-p", str(PORT), "-a", "127.0.0.1", "-g-40", "-o", str(log), str(path)])
    ws = None
    try:
        for _ in range(50):
            try:
                ws = ws_sync.connect(f"ws://127.0.0.1:{PORT}", open_timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        assert ws is not None, "engine websocket did not come up"

        def cmd(name, **fields):
            ws.send(json.dumps({"command": name, **fields}))
            reply = json.loads(ws.recv(timeout=5))
            assert reply["reply"] == name
            return reply

        yield cmd, log
    finally:
        if ws is not None:
            ws.close()
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _send(cmd, op):
    c = d.op_to_command(op)
    return cmd(c.pop("command"), **c)


def _read_back(cmd, ptr):
    """GetConfigValue, polled: measured on 5.0.0-beta.1, a read issued right after an Ok write
    can still return the previous value for a few ms (mostly in the first second after start);
    the write is never lost, so poll briefly instead of asserting on the first read."""
    return cmd("GetConfigValue", value=ptr)["value"]


def _eventually(cmd, ptr, expect, timeout=0.5):
    t = time.monotonic()
    while True:
        got = _read_back(cmd, ptr)
        if got == expect or time.monotonic() - t > timeout:
            return got
        time.sleep(0.005)


@engine
def test_engine_applies_every_op_type(engine_ws):
    cmd, log = engine_ws
    assert cmd("GetState")["value"] == "Running"
    s0 = State()
    s1 = s0.copy()
    s1.live_peq[0].gain, s1.live_peq[0].freq, s1.live_peq[0].q = -6.0, 3150.0, 8.0
    s1.sys_peq[2].gain = -2.5
    s1.live_lowshelf.gain = 3.0
    s1.input_trim_db, s1.sub_delay_ms, s1.top_delay_ms = 2.0, 2.5, 1.0
    s1.sub_inverted, s1.lowcut_on, s1.sub_lowpass_on = True, False, True
    s1.out_trim_db[2] = -3.0
    s1.balance_db, s1.master_db, s1.muted = 4.0, -12.0, True
    ops = d.diff_ops(s0, s1)
    kinds = {type(o) for o in ops}
    assert kinds == {d.SetValue, d.Fader, d.FaderMute}
    for op in ops:
        r = _send(cmd, op)
        assert r["result"] == "Ok", (op, r)
        if isinstance(op, d.SetValue):
            assert _eventually(cmd, op.pointer, op.value) == op.value, op
    faders = cmd("GetFaders")["value"]
    assert faders[0] == {"volume": -12.0, "mute": True}
    assert faders[1]["volume"] == 2.0 and faders[2]["volume"] == -2.0
    # parameter writes update filters in place: no pipeline rebuild
    text = log.read_text()
    assert "Updating parameters of filters" in text and "Rebuilding pipeline" not in text

    # adoption + drift on the live engine
    time.sleep(0.05)
    live = cmd("GetConfigJson")["value"]
    adopted = d.state_from_config(live, faders, State())
    assert adopted == s1.clamped()
    assert d.config_drift(live, d.signal_generator_variant(d.generate(dev(), adopted))).clean
    assert d.fader_drift(faders, adopted) == []

    # Rebuild: mono subs via a whole-config upload
    s2 = s1.copy()
    s2.mono_subs = True
    ops = d.diff_ops(s1, s2)
    assert isinstance(ops[0], d.Rebuild)
    r = cmd("SetConfigJson", value=json.dumps(d.signal_generator_variant(d.generate(dev(), s2))))
    assert r["result"] == "Ok", r
    time.sleep(0.3)
    sources = cmd("GetConfigValue", value="/mixers/split/mapping/0/sources")["value"]
    assert [(x["channel"], x["gain"]) for x in sources] == [(0, -6.0), (1, -6.0)]
    assert "Rebuilding pipeline" in log.read_text()
    assert cmd("GetState")["value"] == "Running"
    assert cmd("GetFaders")["value"][0] == {"volume": -12.0, "mute": True}   # faders survive a rebuild


@engine
def test_engine_smooth_plan_and_patch(engine_ws):
    cmd, _log = engine_ws
    a = State()
    for b in a.live_peq:
        b.gain = -6.0
    r = cmd("PatchConfig", value=d.ops_to_patch(d.diff_ops(State(), a)))
    assert r["result"] == "Ok", r
    b = a.live_reset()
    b.lowcut_on = False
    b.top_inverted = True
    for wait, batch in d.smooth_ops(a, b):
        time.sleep(wait)
        sets = [o for o in batch if isinstance(o, d.SetValue)]
        if len(sets) > 1:
            assert cmd("PatchConfig", value=d.ops_to_patch(sets))["result"] == "Ok"
        elif sets:
            assert _send(cmd, sets[0])["result"] == "Ok"
        for o in batch:
            if not isinstance(o, d.SetValue):
                assert _send(cmd, o)["result"] == "Ok"
    for ptr, val in d.param_map(b).items():
        assert _eventually(cmd, ptr, val) == val, ptr
    assert cmd("GetFaders")["value"][2]["mute"] is False          # guard unmuted the tops again


@engine
def test_engine_rejects_out_of_range_as_last_defence(engine_ws):
    cmd, _log = engine_ws
    r = cmd("SetConfigValue", pointer="/filters/top_lowcut/parameters/freq", value=30000.0)
    assert r["result"] == "ConfigValidationError"
    time.sleep(0.05)
    assert _read_back(cmd, "/filters/top_lowcut/parameters/freq") == 100.0


# ============================================================================ review fixes (unit)
# --- finding: the gig pin must not fall back silently to dev
@pytest.mark.parametrize("raw", ["M4", " m4 ", "m4\n", "M4 "])
def test_profile_name_is_normalised(raw):
    c = pf.choose_profile({"profile": raw}, "dev")
    assert c == pf.ProfileChoice("m4", "settings", None)
    assert pf.load_profile(raw, probe=False).name == "m4"


def test_unknown_profile_is_an_error_and_prefers_last_running():
    c = pf.choose_profile({"profile": "m5"}, "m4")
    assert (c.name, c.source) == ("m4", "state") and "unknown profile 'm5'" in c.error
    c = pf.choose_profile({"profile": "m5"}, None)
    assert (c.name, c.source) == ("dev", "default") and c.error
    assert pf.choose_profile({}, "m4") == pf.ProfileChoice("m4", "state", None)   # no pin: no error
    assert pf.choose_profile("garbage", None) == pf.ProfileChoice("dev", "default", None)


def test_broken_settings_file_keeps_the_pin_and_flags_it(tmp_path):
    f = tmp_path / "k1000.yaml"
    shipped = (REPO_BOX / "etc" / "k1000.yaml").read_text().replace("profile: dev", "profile: m4")
    assert "profile: m4" in shipped
    f.write_text(shipped.replace("  dev:\n", "  dev:\n\tprobe: false\n"))   # a tab while editing
    settings = pf.load_settings(f)
    assert pf.SETTINGS_ERROR in settings and settings["profile"] == "m4"     # pin rescued
    c = pf.choose_profile(settings, "dev")
    assert c.name == "m4" and c.source == "settings" and "does not parse" in c.error
    p = pf.load_profile(c.name, settings, probe=False)
    assert p.name == "m4" and any("does not parse" in w for w in p.warnings)
    f.write_text("profile: 'M4 '  # gig\ncontrols: [\n")                       # quoted, odd case
    assert pf.choose_profile(pf.load_settings(f), None).name == "m4"
    f.write_text("controls: [\n")                                             # no pin at all
    c = pf.choose_profile(pf.load_settings(f), "m4")
    assert (c.name, c.source) == ("m4", "state") and c.error                  # last running, flagged


@pytest.mark.parametrize("ov", [
    {"profiles": {"m4": "hw:M4"}}, {"profiles": {"m4": ["hw:M4"]}}, {"profiles": {"m4": 4}},
    {"profiles": "m4"}, {"profiles": ["m4"]}, ["hw:M4"], "hw:M4", 4,
    {"profiles": {"m4": {"input_map": [float("inf"), 3], "output_map": 7, "capture_channels": "x"}}},
])
def test_malformed_overrides_never_raise(ov):
    p = pf.load_profile("m4", ov, probe=False)
    assert p.capture_device == "hw:M4" and p.input_map == (2, 3) and p.output_map == (2, 3, 0, 1)
    assert p.warnings


# --- finding: the devices block must not depend on whether the probe ran
def test_m4_devices_block_is_the_same_probed_or_not():
    hw = pf.HwParams(4, 4, ["S32_LE", "S24_3_LE"], ["S32_LE", "S24_3LE"], 44100, 192000)
    probed = pf.load_profile("m4", {}, prober=_fake_prober(pf.AlsaProbe("hw:M4", True, playback=hw, capture=hw)))
    absent = pf.load_profile("m4", {}, prober=_fake_prober(pf.AlsaProbe("hw:M4", False)))
    busy = pf.load_profile("m4", {}, prober=_fake_prober(pf.AlsaProbe("hw:M4", True, busy=True)))
    cfgs = [d.generate(p, State(profile="m4"))["devices"] for p in (m4(), probed, absent, busy)]
    assert all(c == cfgs[0] for c in cfgs)
    assert "format" not in cfgs[0]["capture"] and "format" not in cfgs[0]["playback"]
    # running config written while unprobed (engine reports format null) vs a probed restart
    live = d.generate(m4(), State(profile="m4"))
    live["devices"]["capture"]["format"] = None
    live["devices"]["playback"]["format"] = None
    assert d.config_drift(live, d.generate(probed, State(profile="m4"))).clean


# --- finding: config_drift ignored device-level keys
@pytest.mark.parametrize("key,val", [("volume_limit", 50.0), ("volume_ramp_time_ms", 0.0),
                                     ("enable_rate_adjust", True), ("target_level", 1024),
                                     ("chunksize", 1024), ("samplerate", 44100),
                                     ("silence_threshold", -60.0), ("resampler", {"type": "Synchronous"})])
def test_config_drift_reports_device_keys_separately(key, val):
    exp = d.generate(dev(), State())
    live = json.loads(json.dumps(exp))
    live["devices"].update(queuelimit=None, multithreaded=None)       # engine fills unset keys with null
    live["devices"][key] = val
    drift = d.config_drift(live, exp)
    assert len(drift.devices) == 1 and key in drift.devices[0]
    assert drift.rebuild == [] and drift.fixes == []                    # never a live "fix": a restart
    assert not drift.clean


def test_config_drift_unit_change_fixes_the_whole_filter():
    exp = d.generate(dev(), State(sub_delay_ms=2.0, input_trim_db=-6.0))
    live = json.loads(json.dumps(exp))
    live["filters"]["sub_delay"]["parameters"].update(delay=96, delay_unit="samples")    # same 2 ms
    live["filters"]["in_trim"]["parameters"].update(gain=0.5012, scale="linear")         # same -6 dB
    drift = d.config_drift(live, exp)
    assert drift.devices == [] and drift.rebuild == []
    assert set(drift.fixes) >= {d.SetValue("/filters/sub_delay/parameters/delay", 2.0),
                                d.SetValue("/filters/sub_delay/parameters/delay_unit", "ms"),
                                d.SetValue("/filters/in_trim/parameters/gain", -6.0),
                                d.SetValue("/filters/in_trim/parameters/scale", "dB")}
    patch = d.ops_to_patch(drift.fixes)                                    # one atomic PatchConfig
    assert patch["filters"]["sub_delay"]["parameters"]["delay_unit"] == "ms"


def test_config_drift_skips_recent_writes():
    exp = d.generate(dev(), State())
    live = json.loads(json.dumps(exp))
    live["filters"]["live_peq_1"]["parameters"]["gain"] = -3.0
    assert d.config_drift(live, exp, skip_pointers=["/filters/live_peq_1/parameters/gain"]).clean


# --- findings: fader drift (hidden Main, stuck guard mute, common-mode Aux)
def _faders(main=-20.0, aux1=0.0, aux2=0.0, main_mute=False, m1=False, m2=False):
    return [{"volume": main, "mute": main_mute}, {"volume": aux1, "mute": m1},
            {"volume": aux2, "mute": m2}, {"volume": 0.0, "mute": False}, {"volume": 0.0, "mute": False}]


def test_hidden_main_above_range_is_pulled_down():
    f = _faders(main=12.0)
    s = d.state_from_config(d.generate(dev(), State()), f, State())
    assert s.master_db == 0.0                                    # adopted clamped
    assert d.fader_drift(f, s) == [d.Fader(0, 0.0)]


def test_fader_drift_never_raises_or_unmutes_main():
    s = State(master_db=-10.0)
    assert d.fader_drift(_faders(main=-40.0, main_mute=True), s) == []     # fresh engine: boot ramp's job
    assert d.fader_drift(_faders(main=-10.0, main_mute=False), State(master_db=-10.0, muted=True)) == [
        d.FaderMute(0, True)]                                                # but it does mute


def test_stuck_guard_mute_is_visible_and_released():
    f = _faders(m1=True)
    s = d.state_from_config(d.generate(dev(), State()), f, State())
    assert s == State()                                          # not adopted as state...
    assert d.fader_drift(f, s) == [d.FaderMute(1, False)]       # ...but released
    assert d.fader_drift(f, s, skip_faders=[1]) == []           # unless K1000's own guard runs
    plan = d.smooth_ops(State(), State(sub_delay_ms=1.0))
    assert plan[0] == (0.0, [d.FaderMute(1, True)])
    assert d.plan_release_ops(plan) == [d.FaderMute(1, False)]
    assert d.plan_release_ops(d.smooth_ops(State(), State(master_db=-3.0))) == []


@pytest.mark.parametrize("aux,adopted_balance,drift", [
    ((6.0, 6.0), 0.0, [d.Fader(1, 0.0), d.Fader(2, 0.0)]),          # common-mode: not adopted, reverted
    ((20.0, 0.0), 0.0, [d.Fader(1, 0.0)]),                           # one-sided: reverted
    ((2.0, -2.0), 4.0, []),                                           # a balance pair: adopted
    ((8.0, -8.0), 12.0, [d.Fader(1, 6.0), d.Fader(2, -6.0)]),        # pair out of range: clamped + pushed
])
def test_aux_adoption_only_for_balance_pairs(aux, adopted_balance, drift):
    f = _faders(aux1=aux[0], aux2=aux[1])
    s = d.state_from_config(d.generate(dev(), State()), f, State())
    assert s.balance_db == adopted_balance
    assert d.fader_drift(f, s) == drift


# --- finding: boot ramp
def test_boot_ramp_plan_follows_the_contract():
    plan = d.boot_ramp_plan(State(master_db=-10.0, balance_db=4.0))
    assert plan[0] == (0.0, [d.Fader(1, 2.0), d.Fader(2, -2.0), d.FaderMute(1, False),
                             d.FaderMute(2, False), d.Fader(0, -40.0)])        # parked while muted
    assert plan[1] == (d.GUARD_WAIT_S, [d.FaderMute(0, False)])
    steps = [b[0].db for _w, b in plan[2:]]
    assert len(steps) == 30 and steps[-1] == -10.0                             # 3 s / 0.1 s engine ramps
    assert all(b > a for a, b in zip([-40.0] + steps, steps))                  # monotonic up
    assert max(b - a for a, b in zip([-40.0] + steps, steps)) <= 3.0 + 1e-9
    assert sum(w for w, _b in plan[2:]) == pytest.approx(3.0)
    assert not any(isinstance(o, d.FaderMute) and o.index == 0 and o.mute is False
                   for _w, b in plan[2:] for o in b)


def test_boot_ramp_plan_edge_cases():
    quiet = d.boot_ramp_plan(State(master_db=-60.0))
    assert quiet == [(0.0, [d.Fader(1, 0.0), d.Fader(2, 0.0), d.FaderMute(1, False), d.FaderMute(2, False),
                            d.Fader(0, -60.0)]), (d.GUARD_WAIT_S, [d.FaderMute(0, False)])]
    muted = d.boot_ramp_plan(State(master_db=-10.0, muted=True))
    assert muted == [(0.0, [d.Fader(1, 0.0), d.Fader(2, 0.0), d.FaderMute(1, False), d.FaderMute(2, False),
                            d.Fader(0, -10.0), d.FaderMute(0, True)])]
    garbage = d.boot_ramp_plan(State(master_db=0.0), start_db="x", ramp_s=float("nan"))
    assert garbage[0][1][-1] == d.Fader(0, -40.0) and garbage[-1][1] == [d.Fader(0, 0.0)]
    s = State()
    s.master_db = 99.0                                                    # unvalidated: clamped
    assert d.boot_ramp_plan(s)[-1][1] == [d.Fader(0, 0.0)]


# --- finding: delays are whole samples
def test_delay_writes_only_when_the_sample_count_changes():
    a = State(sub_delay_ms=1.0)
    assert d.diff_ops(a, State(sub_delay_ms=1.009)) == []                 # 48 -> 48 samples
    assert d.smooth_ops(a, State(sub_delay_ms=1.009)) == []               # so no guard dip either
    up = a.copy()
    up.nudge("sub_delay_ms", 1, fine=True)
    assert d.diff_ops(a, up) == [d.SetValue("/filters/sub_delay/parameters/delay", round(49 / 48, 6))]
    assert d.param_map(State(sub_delay_ms=0.1))["/filters/sub_delay/parameters/delay"] == round(5 / 48, 6)


def test_delay_adoption_and_drift_work_in_samples():
    base = State(sub_delay_ms=1.0)
    live = d.generate(dev(), base)
    live["filters"]["sub_delay"]["parameters"]["delay"] = 1.009          # a GUI value, same 48 samples
    s = d.state_from_config(live, None, base)
    assert s.sub_delay_ms == 1.0                                         # state keeps its own value
    assert d.config_drift(live, d.generate(dev(), s)).clean              # and nothing is rewritten
    live["filters"]["sub_delay"]["parameters"]["delay"] = 1.5            # a real change: 72 samples
    s = d.state_from_config(live, None, base)
    assert s.sub_delay_ms == 1.5 and d.config_drift(live, d.generate(dev(), s)).clean


# ============================================================================ review fixes (engine)
@contextlib.contextmanager
def _engine(tmp_path, cfg, *args):
    """A private camilladsp on PORT with `cfg` (never the ALSA card); yields (cmd, log_path)."""
    ws_sync = pytest.importorskip("websockets.sync.client")
    if CDSP is None:
        pytest.skip("camilladsp binary not installed")
    if not _port_free(PORT):
        pytest.skip(f"port {PORT} busy")
    path = tmp_path / "engine2.yml"
    path.write_text(d.to_yaml(cfg))
    log = tmp_path / "engine2.log"
    proc = subprocess.Popen([CDSP, "-v", "-p", str(PORT), "-a", "127.0.0.1", *args, "-o", str(log), str(path)])
    ws = None
    try:
        for _ in range(50):
            try:
                ws = ws_sync.connect(f"ws://127.0.0.1:{PORT}", open_timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        assert ws is not None, "engine websocket did not come up"

        def cmd(name, **fields):
            ws.send(json.dumps({"command": name, **fields}))
            reply = json.loads(ws.recv(timeout=5))
            assert reply["reply"] == name
            return reply

        yield cmd, log
    finally:
        if ws is not None:
            ws.close()
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _run_plan(cmd, plan):
    for wait, batch in plan:
        time.sleep(wait)
        sets = [o for o in batch if isinstance(o, d.SetValue)]
        if len(sets) > 1:
            assert cmd("PatchConfig", value=d.ops_to_patch(sets))["result"] == "Ok"
        elif sets:
            assert _send(cmd, sets[0])["result"] == "Ok"
        for o in batch:
            if not isinstance(o, d.SetValue):
                assert _send(cmd, o)["result"] == "Ok", o


def _fader_eventually(cmd, idx, key, expect, timeout=1.0):
    t = time.monotonic()
    while True:
        got = cmd("GetFaders")["value"][idx][key]
        if got == expect or time.monotonic() - t > timeout:
            return got
        time.sleep(0.01)


@engine
def test_engine_hidden_main_is_pulled_down_and_volume_limit_is_a_device_change(engine_ws):
    cmd, log = engine_ws
    assert _send(cmd, d.Fader(0, 12.0))["result"] == "Ok"            # e.g. a GUI write; volume_limit hides it
    assert _fader_eventually(cmd, 0, "volume", 12.0) == 12.0
    faders = cmd("GetFaders")["value"]
    live = cmd("GetConfigJson")["value"]
    adopted = d.state_from_config(live, faders, State())
    assert adopted.master_db == 0.0
    ops = d.fader_drift(faders, adopted)
    assert ops == [d.Fader(0, 0.0)]
    for op in ops:
        assert _send(cmd, op)["result"] == "Ok"
    assert _fader_eventually(cmd, 0, "volume", 0.0) == 0.0
    # lifting volume_limit (as a GUI config Apply would) restarts the devices: report, never "fix"
    n_restarts = log.read_text().count("Devices changed, restart required")
    assert cmd("SetConfigValue", pointer="/devices/volume_limit", value=50.0)["result"] == "Ok"
    time.sleep(0.5)
    assert log.read_text().count("Devices changed, restart required") == n_restarts + 1
    assert cmd("GetState")["value"] == "Running"
    live = cmd("GetConfigJson")["value"]
    drift = d.config_drift(live, d.signal_generator_variant(d.generate(dev(), adopted)))
    assert drift.devices == ["devices.volume_limit 50.0 != 0.0"] and drift.rebuild == [] and drift.fixes == []
    # a structure fix while devices differ: keep the running devices -> pipeline rebuild only
    mono = adopted.copy()
    mono.mono_subs = True
    expected = d.signal_generator_variant(d.generate(dev(), mono))
    drift = d.config_drift(live, expected)
    assert drift.rebuild == ["mixer split differs"] and drift.devices
    text0 = log.read_text()
    assert cmd("SetConfigJson", value=json.dumps(d.with_live_devices(expected, live)))["result"] == "Ok"
    time.sleep(0.5)
    new = log.read_text()[len(text0):]
    assert "Rebuilding pipeline" in new and "Devices changed" not in new
    drift = d.config_drift(cmd("GetConfigJson")["value"], expected)
    assert drift.devices == ["devices.volume_limit 50.0 != 0.0"] and drift.rebuild == [] and drift.fixes == []


@engine
def test_engine_stuck_guard_mute_is_released(engine_ws):
    cmd, _log = engine_ws
    plan = d.smooth_ops(State(), State(sub_delay_ms=1.0))
    _run_plan(cmd, plan[:1])                                          # "crash" right after the guard mute
    assert _fader_eventually(cmd, 1, "mute", True) is True
    faders = cmd("GetFaders")["value"]
    adopted = d.state_from_config(cmd("GetConfigJson")["value"], faders, State())
    ops = d.fader_drift(faders, adopted)
    assert ops == [d.FaderMute(1, False)] == d.plan_release_ops(plan)
    for op in ops:
        assert _send(cmd, op)["result"] == "Ok"
    assert _fader_eventually(cmd, 1, "mute", False) is False


@engine
def test_engine_delay_is_whole_samples(engine_ws):
    cmd, log = engine_ws
    ptr = "/filters/sub_delay/parameters/delay"

    def built():
        time.sleep(0.15)
        found = re.findall(r"Building delay filter 'sub_delay' with delay (\d+) samples", log.read_text())
        return int(found[-1]) if found else None

    for ms in (0.0104, 0.0105, 0.031249, 0.03125, 0.104167, 1.009):    # the model mirrors the engine
        assert cmd("SetConfigValue", pointer=ptr, value=ms)["result"] == "Ok"
        assert built() == delay_samples(ms), ms
    a = State(sub_delay_ms=1.0)
    for op in d.diff_ops(State(), a):
        assert _send(cmd, op)["result"] == "Ok"
    assert built() == 48
    b = a.copy()
    b.nudge("sub_delay_ms", 1, fine=True)                             # one fine detent = one sample
    ops = d.diff_ops(a, b)
    assert len(ops) == 1
    n_builds = log.read_text().count("Building delay filter 'sub_delay'")
    assert _send(cmd, ops[0])["result"] == "Ok"
    assert built() == 49
    assert log.read_text().count("Building delay filter 'sub_delay'") > n_builds
    assert d.diff_ops(b, State(sub_delay_ms=b.sub_delay_ms + 0.004)) == []   # same sample: nothing sent


@engine
def test_engine_boot_ramp_plan_from_muted_boot(tmp_path):
    cfg = d.signal_generator_variant(d.generate(dev(), State()))
    with _engine(tmp_path, cfg, "-m", "-g-40") as (cmd, _log):        # as camilladsp.service boots
        time.sleep(0.3)
        assert cmd("GetFaders")["value"][0] == {"volume": -40.0, "mute": True}
        s = State(master_db=-10.0, balance_db=4.0)
        assert not any(isinstance(o, (d.Fader, d.FaderMute)) and o.index == 0
                       for o in d.fader_drift(cmd("GetFaders")["value"], s))   # never jumps Main up
        plan = d.boot_ramp_plan(s, ramp_s=0.6)
        _run_plan(cmd, plan[:1])
        f = cmd("GetFaders")["value"]
        assert f[0] == {"volume": -40.0, "mute": True}                # parked, still muted
        assert f[1] == {"volume": 2.0, "mute": False} and f[2] == {"volume": -2.0, "mute": False}
        _run_plan(cmd, plan[1:])
        assert cmd("GetFaders")["value"][0] == {"volume": -10.0, "mute": False}
        assert d.fader_drift(cmd("GetFaders")["value"], s) == []


@engine
def test_engine_alsa_auto_format_is_not_device_drift(tmp_path):
    """Finding: unprobed m4 (format omitted) vs a probed restart must not differ. Uses the ALSA
    `null` PCM (software, no hardware) for capture; never hw:sndrpihifiberry."""
    hw = pf.HwParams(4, 4, ["S32_LE", "S24_3_LE"], ["S32_LE", "S24_3LE"], 44100, 192000)
    probed = pf.load_profile("m4", {"capture_device": "null", "playback_device": "null"}, probe=True,
                             prober=_fake_prober(pf.AlsaProbe("null", True, playback=hw, capture=hw)))
    unprobed = pf.load_profile("m4", {"capture_device": "null", "playback_device": "null"}, probe=False)
    running = d.generate(unprobed, State(profile="m4"))
    running["devices"]["playback"] = {"type": "File", "channels": 4, "filename": "/dev/null", "format": "S32_LE"}
    with _engine(tmp_path, running) as (cmd, _log):
        t = time.monotonic()
        while cmd("GetState")["value"] != "Running" and time.monotonic() - t < 3:
            time.sleep(0.1)
        if cmd("GetState")["value"] != "Running":
            pytest.skip("ALSA null capture did not start here")
        live = json.loads(cmd("GetConfigJson")["value"])
        assert live["devices"]["capture"]["type"] == "Alsa" and live["devices"]["capture"]["format"] is None
        expected = d.generate(probed, State(profile="m4"))
        expected["devices"]["playback"] = running["devices"]["playback"]
        drift = d.config_drift(live, expected)
        assert drift.devices == [], drift.devices                     # incl. no non-null extra Alsa keys
        assert drift.clean


def test_with_live_devices_keeps_running_devices_only():
    exp = d.generate(dev(), State(mono_subs=True))
    live = d.generate(dev(), State())
    live["devices"]["volume_limit"] = 50.0
    up = d.with_live_devices(exp, json.dumps(live))
    assert up["devices"]["volume_limit"] == 50.0 and up["mixers"] == exp["mixers"]
    assert exp["devices"]["volume_limit"] == 0.0                         # expected untouched
    assert d.with_live_devices(exp, "garbage") == exp


# ============================================================================ D-014 band IN/OUT
def _cut(on: bool = True, slot: int = 1, gain: float = -6.5) -> State:
    s = State()
    b = s.live_peq[slot]
    b.freq, b.gain, b.q, b.on = 2520.0, gain, 8.0, on
    return s


def test_an_out_band_is_written_as_0_db_with_its_freq_and_q_kept():
    on, out = _cut(), _cut(on=False)
    g = d._pp("live_peq_2", "gain")
    assert d.param_map(on)[g] == -6.5 and d.param_map(out)[g] == 0.0
    for k in ("freq", "q"):
        p = d._pp("live_peq_2", k)
        assert d.param_map(on)[p] == d.param_map(out)[p]
    filt = d.generate(dev(), out)["filters"]["live_peq_2"]["parameters"]
    assert (filt["freq"], filt["gain"], filt["q"]) == (2520.0, 0.0, 8.0)
    # shelves too
    s = State()
    s.live_lowshelf.gain, s.live_lowshelf.on = 4.0, False
    assert d.param_map(s)[d._pp("live_lowshelf", "gain")] == 0.0
    # an OUT band at 0 dB writes exactly what an IN one does: nothing to send
    flat_out = State()
    flat_out.live_peq[0].on = False
    assert d.diff_ops(State(), flat_out) == []


def test_band_out_and_in_is_a_smoothed_gain_move_never_a_rebuild():
    on, out = _cut(), _cut(on=False)
    g = d._pp("live_peq_2", "gain")
    for a, b, start, end in ((on, out, -6.5, 0.0), (out, on, 0.0, -6.5)):
        ops = d.diff_ops(a, b)
        assert ops == [d.SetValue(g, end)]                              # one value, no Rebuild
        plan = d.smooth_ops(a, b)
        vals = [op.value for op in _flatten(plan)]
        assert all(isinstance(op, d.SetValue) and op.pointer == g for op in _flatten(plan))
        assert len(vals) == 13 and vals[-1] == end                      # 6.5 dB in 0.5 dB steps
        prev = start
        for v in vals:
            assert abs(v - prev) <= 0.5 + 1e-9
            prev = v
        assert plan[0][0] == 0.0 and all(w == d.STEP_WAIT_S for w, _ in plan[1:])
    # exactly the plan of the same gain edit
    zero = _cut(gain=0.0)
    assert d.smooth_ops(on, out) == d.smooth_ops(on, zero)


def test_adoption_keeps_an_out_band_and_its_stored_gain():
    out = _cut(on=False)
    cfg = d.generate(dev(), out)                                        # the engine plays 0 dB
    s = d.state_from_config(cfg, None, out)
    assert s.live_peq[1].on is False and s.live_peq[1].gain == -6.5
    assert s == out.clamped()
    assert d.config_drift(cfg, d.generate(dev(), s)).clean
    # CamillaGUI puts a gain on the OUT band: that is what plays, so it is adopted, and IN
    cfg["filters"]["live_peq_2"]["parameters"]["gain"] = -3.0
    s = d.state_from_config(cfg, None, out)
    assert s.live_peq[1].on is True and s.live_peq[1].gain == -3.0
    # a value K1000 wrote itself moments ago is skipped as usual: the band stays as it was
    s = d.state_from_config(cfg, None, out, skip_pointers=[d._pp("live_peq_2", "gain")])
    assert s.live_peq[1].on is False and s.live_peq[1].gain == -6.5
    # an IN band adopts the engine's gain, as before
    on = _cut()
    cfg = d.generate(dev(), on)
    cfg["filters"]["live_peq_2"]["parameters"]["gain"] = 0.0
    s = d.state_from_config(cfg, None, on)
    assert s.live_peq[1].on is True and s.live_peq[1].gain == 0.0
