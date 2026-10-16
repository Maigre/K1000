# SPDX-License-Identifier: GPL-3.0-or-later
"""dspconfig + profiles: config generation, live diff ops, adoption, drift, click-safe plans.

Unit tests need no hardware. Tests marked `engine` run the real camilladsp binary (on the box):
`camilladsp -c` validity of generated configs, and a private instance on a SignalGenerator ->
File(/dev/null) config (never the ALSA card) driven over the v5 websocket, or an offline run
(RawFile capture -> File playback, no websocket) whose output is measured sample by sample.
Port: $K1000_DSPCONFIG_PORT (default 12341).
"""
import contextlib
import copy
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

import dspconfig_golden as golden
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


def _used_capture_channels(cfg):
    """CamillaDSP 5.0.0-beta.1 used_capture_channels + mixer::used_input_channels: the first
    mixer's non-muted sources in non-muted mappings."""
    im = cfg["mixers"][next(st["name"] for st in cfg["pipeline"] if st["type"] == "Mixer")]
    used = [False] * im["channels"]["in"]
    for m in im["mapping"]:
        if not m.get("mute"):
            for s in m["sources"]:
                if not s.get("mute"):
                    used[s["channel"]] = True
    return used


@pytest.mark.parametrize("settings", [
    {},
    {"profiles": {"m4": {"input_map": [0, 1]}}},
    {"profiles": {"m4": {"capture_channels": 6}}},
])
def test_every_capture_channel_is_used_and_adds_nothing(settings):
    """The engine must fill every capture channel: its spectrum ring buffer panics on an unused
    (empty) one (2026-10-16, M4: LINE IN 3/4 used, mic inputs 1-2 not, restart every 2 s). The
    extra sources add exactly nothing (linear gain 0) and leave R alone."""
    prof = pf.load_profile("m4", settings, probe=False)
    l, r = prof.input_map
    cfg = d.generate(prof, State(profile="m4"))
    assert _used_capture_channels(cfg) == [True] * prof.capture_channels
    im = cfg["mixers"]["input"]
    left, right = im["mapping"][0]["sources"], im["mapping"][1]["sources"]
    assert (left[0]["channel"], left[0]["gain"], left[0]["scale"]) == (l, 0.0, "dB")
    assert len(left) == prof.capture_channels - 1
    assert all(s["gain"] == 0.0 and s["scale"] == "linear" for s in left[1:])
    assert right == [{"channel": r, "gain": 0.0, "inverted": False, "scale": "dB"}]


def test_dev_input_mixer_unchanged():
    im = d.generate(dev(), State())["mixers"]["input"]
    assert [[s["channel"] for s in m["sources"]] for m in im["mapping"]] == [[0], [1]]


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


# ============================================================================ 2026-10-16 AUX / loudness / de-harsh
# Thomas after the first rig run: a band compressor for the hard 2.7 kHz attacks (de-harsh), subs
# that stay full at low level (sub loudness), and IN1/IN2 into the program (AUX IN). All off by
# default; with all three off the config must be f2671b0's, byte for byte (Sunday safety).
FEATURE_VALUES = dict(aux_gain_db=-12.5, loud_boost_db=12.0, loud_ref_db=-30.0, deharsh_hz=4100.0,
                      deharsh_thresh_db=-6.0)
AUX_GAIN_PTR = "/filters/aux_gain/parameters/gain"
DH_FREQ_PTR = "/filters/dh_band/parameters/freq"
DH_THRESH_PTR = "/processors/dh_comp/parameters/threshold"
LOUD_REF_PTR = "/filters/sub_loudness/parameters/reference_level"
LOUD_BOOST_PTR = "/filters/sub_loudness/parameters/low_boost"
FEATURE_PTRS = (AUX_GAIN_PTR, DH_FREQ_PTR, DH_THRESH_PTR, LOUD_REF_PTR, LOUD_BOOST_PTR)


def feature_profiles():
    """dev (no spare input), m4 (IN1/IN2 spare), and the m4 variants that move the spares."""
    return {
        "dev": dev(),
        "m4": m4(),
        "m4_6ch": m4(capture_channels=6),                 # spares 0, 1, 4, 5
        "m4_in12": m4(input_map=[0, 1]),                  # program on IN1/2: spares 2, 3
        "m4_3ch": m4(capture_channels=3, input_map=[1, 2]),   # one spare: line degrades to mono
    }


def feature_states(profile_name="dev"):
    """Every combination of the three switches x mono subs, with non-default feature values."""
    out = {}
    for aux in ("off", "mic", "line"):
        for loud in (False, True):
            for dh in (False, True):
                for mono in (False, True):
                    s = State(profile=profile_name, aux_mode=aux, loud_on=loud, deharsh_on=dh,
                              mono_subs=mono, **FEATURE_VALUES)
                    out[f"aux_{aux}{'_loud' if loud else ''}{'_dh' if dh else ''}{'_mono' if mono else ''}"] = s
    return out


def _channel_flow(cfg):
    """camilladsp's validate_pipeline channel bookkeeping, plus K1000's own rules: every mixer
    feeds each of its outputs, every step names something defined, processors match the count."""
    n = cfg["devices"]["capture"]["channels"]
    for st in cfg["pipeline"]:
        if st["type"] == "Mixer":
            m = cfg["mixers"][st["name"]]
            assert m["channels"]["in"] == n, st
            assert sorted(mp["dest"] for mp in m["mapping"]) == list(range(m["channels"]["out"])), st
            assert all(0 <= src["channel"] < n for mp in m["mapping"] for src in mp["sources"]), st
            assert len(m["labels"]) == m["channels"]["out"], st
            n = m["channels"]["out"]
        elif st["type"] == "Filter":
            assert st["channels"] and all(0 <= c < n for c in st["channels"]), st
            assert len(set(st["channels"])) == len(st["channels"]), st
            assert all(name in cfg["filters"] for name in st["names"]), st
        else:
            assert st["type"] == "Processor", st
            par = cfg["processors"][st["name"]]["parameters"]
            assert par["channels"] == n, st
            assert all(0 <= c < n for c in par["monitor_channels"] + par["process_channels"]), st
    assert n == cfg["devices"]["playback"]["channels"]


def _steps(cfg):
    return [(s["type"], s.get("name"), tuple(s.get("names") or ()), tuple(s.get("channels") or ()))
            for s in cfg["pipeline"]]


def _srcs(mixer, dest):
    m = next(m for m in mixer["mapping"] if m["dest"] == dest)
    return [(x["channel"], x["gain"], x["inverted"], x["scale"]) for x in m["sources"]]


# ----------------------------------------------------------------- Sunday safety: f2671b0 identity
@pytest.mark.parametrize("profile_key,state_key", golden.cases())
def test_features_off_config_is_byte_identical_to_f2671b0(profile_key, state_key):
    """THE hard rule: all three off -> exactly the YAML f2671b0 wrote (fixtures captured from a
    `git archive f2671b0` copy before the features existed), whatever the features' own values."""
    want = golden.golden_path(profile_key, state_key).read_text(encoding="utf-8")
    assert golden.render(d, pf, State, profile_key, state_key) == want
    name, ov = golden.PROFILES[profile_key]
    prof = pf.load_profile(name, ov or None, probe=False)
    s = golden.STATES[state_key](State)
    assert (s.aux_mode, s.loud_on, s.deharsh_on) == ("off", False, False)
    for k, v in FEATURE_VALUES.items():
        setattr(s, k, v)
    assert d.to_yaml(d.generate(prof, s)) == want
    assert d.to_yaml(d.generate(prof, s), yaml.SafeDumper) == want


def test_features_off_param_map_is_f2671b0s():
    """The owned pointers (diff_ops, adoption, drift all derive from them) are f2671b0's too."""
    assert golden.render_params(d, State) == golden.params_path().read_text(encoding="utf-8")
    for name, f in golden.STATES.items():
        s = f(State)
        base = d.param_map(s)
        for k, v in FEATURE_VALUES.items():
            setattr(s, k, v)
        assert d.param_map(s) == base, name
        assert d.diff_ops(f(State), s) == [], name           # a hidden feature value sends nothing


def test_golden_fixtures_are_complete():
    files = sorted(p.name for p in golden.GOLDEN_DIR.iterdir())
    assert files == sorted([golden.golden_path(*c).name for c in golden.cases()] + ["param_map.json"])


# ----------------------------------------------------------------- every combination is consistent
@pytest.mark.parametrize("prof_key", list(feature_profiles()))
def test_every_feature_combination_is_a_consistent_config(prof_key):
    prof = feature_profiles()[prof_key]
    for name, s in feature_states(prof.name).items():
        cfg = d.generate(prof, s)
        _channel_flow(cfg)
        assert _used_capture_channels(cfg) == [True] * prof.capture_channels, (prof_key, name)
        for ptr, val in d.param_map(s).items():                 # owned pointers always resolve
            assert resolve(cfg, ptr) == val, (prof_key, name, ptr)
        assert set(FEATURE_PTRS) & set(d.param_map(s)) == {
            p for p, on in ((AUX_GAIN_PTR, s.aux_mode != "off"), (DH_FREQ_PTR, s.deharsh_on),
                            (DH_THRESH_PTR, s.deharsh_on), (LOUD_REF_PTR, s.loud_on),
                            (LOUD_BOOST_PTR, s.loud_on)) if on}
        text = d.to_yaml(cfg)
        assert yaml.safe_load(text) == cfg
        assert d.to_yaml(cfg, yaml.SafeDumper) == text, (prof_key, name)
        assert ("processors" in cfg) is s.deharsh_on
        assert d.config_drift(json.dumps(cfg), cfg).clean, (prof_key, name)
        assert d.state_from_config(json.dumps(cfg), None, s) == s.clamped(), (prof_key, name)


# ----------------------------------------------------------------- AUX IN
def test_aux_m4_mic_routes_in1_centred_after_the_trim_and_before_the_eq():
    cfg = d.generate(m4(), State(profile="m4", aux_mode="mic", aux_gain_db=-4.0))
    im = cfg["mixers"]["input"]
    assert im["channels"] == {"in": 4, "out": 4} and im["labels"] == ["L", "R", "AUX IN1", "AUX IN2"]
    assert [_srcs(im, i) for i in range(4)] == [[(2, 0.0, False, "dB")], [(3, 0.0, False, "dB")],
                                                [(0, 0.0, False, "dB")], [(1, 0.0, False, "dB")]]
    am = cfg["mixers"]["aux_mix"]
    assert am["channels"] == {"in": 4, "out": 2}
    assert _srcs(am, 0) == [(0, 0.0, False, "dB"), (2, -3.0, False, "dB")]
    assert _srcs(am, 1) == [(1, 0.0, False, "dB"), (2, -3.0, False, "dB")]
    f = cfg["filters"]
    assert f["aux_gain"]["parameters"] == {"gain": -4.0, "inverted": False, "mute": False, "scale": "dB"}
    assert f["aux_hp"]["parameters"] == {"type": "Highpass", "freq": 100.0, "q": 0.707}
    st = _steps(cfg)
    assert st[:5] == [("Mixer", "input", (), ()), ("Filter", None, ("in_trim",), (0, 1)),
                      ("Filter", None, ("aux_gain", "aux_hp"), (2,)), ("Mixer", "aux_mix", (), ()),
                      ("Filter", None, d.PROGRAM_EQ, (0, 1))]
    assert st[5] == ("Mixer", "split", (), ())


def test_aux_m4_line_routes_in1_left_in2_right():
    cfg = d.generate(m4(), State(profile="m4", aux_mode="line"))
    am = cfg["mixers"]["aux_mix"]
    assert _srcs(am, 0) == [(0, 0.0, False, "dB"), (2, 0.0, False, "dB")]
    assert _srcs(am, 1) == [(1, 0.0, False, "dB"), (3, 0.0, False, "dB")]
    assert "aux_hp" not in cfg["filters"]
    assert _steps(cfg)[2] == ("Filter", None, ("aux_gain",), (2, 3))
    # mic <-> line keeps the input mixer
    assert cfg["mixers"]["input"] == d.generate(m4(), State(profile="m4", aux_mode="mic"))["mixers"]["input"]


@pytest.mark.parametrize("prof,aux,pads", [
    (m4(capture_channels=6), (0, 1), [4, 5]),     # the first two spares; the others still pad L
    (m4(input_map=[0, 1]), (2, 3), []),           # program on IN1/2: the AUX is IN3/4
])
def test_aux_takes_the_first_two_spares_and_the_rest_still_pad(prof, aux, pads):
    assert d.aux_inputs(prof, "line") == aux == d.aux_inputs(prof, "mic")
    im = d.generate(prof, State(profile="m4", aux_mode="line"))["mixers"]["input"]
    l, r = prof.input_map
    assert _srcs(im, 0) == [(l, 0.0, False, "dB")] + [(c, 0.0, False, "linear") for c in pads]
    assert _srcs(im, 1) == [(r, 0.0, False, "dB")]
    assert [_srcs(im, 2 + i) for i in range(2)] == [[(c, 0.0, False, "dB")] for c in aux]


def test_aux_line_with_a_single_spare_is_a_centred_mono_line():
    prof = m4(capture_channels=3, input_map=[1, 2])
    assert d.spare_capture_channels(prof) == (0,) and d.aux_inputs(prof, "line") == (0,)
    cfg = d.generate(prof, State(profile="m4", aux_mode="line"))
    assert cfg["mixers"]["input"]["channels"] == {"in": 3, "out": 3}
    am = cfg["mixers"]["aux_mix"]
    assert am["channels"] == {"in": 3, "out": 2}
    assert _srcs(am, 0) == [(0, 0.0, False, "dB"), (2, -3.0, False, "dB")]
    assert _srcs(am, 1) == [(1, 0.0, False, "dB"), (2, -3.0, False, "dB")]
    assert "aux_hp" not in cfg["filters"] and _steps(cfg)[2] == ("Filter", None, ("aux_gain",), (2,))


@pytest.mark.parametrize("mode", ["mic", "line"])
def test_aux_on_dev_defines_its_filters_but_routes_nothing(mode):
    """dev (HiFiBerry, both inputs are the program) has no spare channel: mixers and pipeline are
    exactly AUX-off's, so it plays the same; the AUX filters exist only so the owned pointer
    `param_map` names resolves (a knob move is a harmless write, never an engine error)."""
    assert d.spare_capture_channels(dev()) == () and d.aux_inputs(dev(), mode) == ()
    on, off = d.generate(dev(), State(aux_mode=mode)), d.generate(dev(), State())
    assert on["mixers"] == off["mixers"] and on["pipeline"] == off["pipeline"]
    extra = {"aux_gain", "aux_hp"} if mode == "mic" else {"aux_gain"}
    assert set(on["filters"]) - set(off["filters"]) == extra
    assert {k: v for k, v in on["filters"].items() if k not in extra} == off["filters"]
    assert resolve(on, AUX_GAIN_PTR) == 0.0


def test_aux_is_off_for_garbage_modes():
    assert d.aux_inputs(m4(), "off") == () and d.aux_inputs(m4(), "usb") == ()
    s = State(profile="m4")
    s.aux_mode = "usb"                         # unvalidated: clamped to "off"
    assert d.to_yaml(d.generate(m4(), s)) == golden.golden_path("m4", "default_m4").read_text()


# ----------------------------------------------------------------- de-harsh
def test_deharsh_structure_is_a_dynamic_peaking_cut_after_the_eq():
    cfg = d.generate(m4(), State(profile="m4", deharsh_on=True, deharsh_hz=3000.0,
                                 deharsh_thresh_db=-20.0))
    sp, sm = cfg["mixers"]["dh_split"], cfg["mixers"]["dh_sum"]
    assert sp["channels"] == {"in": 2, "out": 6}
    assert [_srcs(sp, i) for i in range(6)] == [[(i % 2, 0.0, False, "dB")] for i in range(6)]
    assert sm["channels"] == {"in": 6, "out": 2}
    assert _srcs(sm, 0) == [(0, 0.0, False, "dB"), (2, 0.0, True, "dB"), (4, 0.0, False, "dB")]
    assert _srcs(sm, 1) == [(1, 0.0, False, "dB"), (3, 0.0, True, "dB"), (5, 0.0, False, "dB")]
    assert cfg["filters"]["dh_band"]["parameters"] == {"type": "Bandpass", "freq": 3000.0, "q": 1.4}
    assert cfg["processors"] == {"dh_comp": {
        "type": "Compressor", "description": cfg["processors"]["dh_comp"]["description"],
        "parameters": {"channels": 6, "monitor_channels": [4, 5], "process_channels": [4, 5],
                       "attack": 1.0, "attack_unit": "ms", "release": 80.0, "release_unit": "ms",
                       "threshold": -20.0, "factor": 3.0, "makeup_gain": 0.0}}}
    assert list(cfg)[-2:] == ["processors", "pipeline"]
    st = _steps(cfg)
    assert st[1] == ("Filter", None, d.INPUT_CHAIN, (0, 1))
    assert st[2:7] == [("Mixer", "dh_split", (), ()), ("Filter", None, ("dh_band",), (2, 3, 4, 5)),
                       ("Processor", "dh_comp", (), ()), ("Mixer", "dh_sum", (), ()),
                       ("Mixer", "split", (), ())]


def test_deharsh_follows_the_aux_so_the_aux_is_de_harshed_too():
    st = _steps(d.generate(m4(), State(profile="m4", deharsh_on=True, aux_mode="mic")))
    names = [s[1] for s in st if s[0] != "Filter"]
    assert names[:6] == ["input", "aux_mix", "dh_split", "dh_comp", "dh_sum", "split"]


# ----------------------------------------------------------------- sub loudness
def test_sub_loudness_follows_the_lowpass_on_the_subs_only():
    cfg = d.generate(m4(), State(profile="m4", loud_on=True, loud_ref_db=-12.0, loud_boost_db=8.0))
    assert cfg["filters"]["sub_loudness"]["type"] == "Loudness"
    assert cfg["filters"]["sub_loudness"]["parameters"] == {
        "fader": "Main", "reference_level": -12.0, "low_boost": 8.0, "high_boost": 0.0,
        "low_freq": 70.0, "attenuate_mid": False}
    st = _steps(cfg)
    assert ("Filter", None, ("sub_trim", "sub_lowpass", "sub_loudness", "sub_delay", "sub_fader"),
            (0, 1)) in st
    assert ("Filter", None, d.TOP_CHAIN, (2, 3)) in st
    assert sum("sub_loudness" in s[2] for s in st) == 1


def test_sub_loudness_is_never_louder_than_at_the_reference():
    """The engine's boost: low_boost * clamp((ref - Main) / 20, 0, 1). At most 0.6 dB per dB of
    master reduction at the range's 12 dB, so the subs' level only ever goes down with Main."""
    rb, rr = RANGES["loud_boost_db"], RANGES["loud_ref_db"]
    for ref in (rr.min, -10.0, rr.max):
        for main in [ref - k for k in range(0, 41)]:
            boost = rb.max * min(max((ref - main) / 20.0, 0.0), 1.0)
            assert main + boost <= ref + 1e-9


# ----------------------------------------------------------------- diff / plans
@pytest.mark.parametrize("field,value", [("aux_mode", "mic"), ("aux_mode", "line"), ("loud_on", True),
                                         ("deharsh_on", True)])
def test_feature_switches_are_rebuilds(field, value):
    for profile in ("dev", "m4"):
        a = State(profile=profile, **FEATURE_VALUES)
        b = a.copy()
        setattr(b, field, value)
        for x, y in ((a, b), (b, a)):
            ops = d.diff_ops(x, y)
            assert isinstance(ops[0], d.Rebuild) and field in ops[0].reason
            assert not any(isinstance(o, d.SetValue) for o in ops)
            assert d.smooth_ops(x, y) == [(0.0, ops)]
    a, b = State(aux_mode="mic"), State(aux_mode="line")
    assert "aux_mode mic -> line" in d.diff_ops(a, b)[0].reason


def test_feature_values_are_owned_setvalues_while_on_and_silent_while_off():
    on = State(profile="m4", aux_mode="line", loud_on=True, deharsh_on=True)
    moved = on.copy()
    for k, v in FEATURE_VALUES.items():
        setattr(moved, k, v)
    assert set(d.diff_ops(on, moved)) == {
        d.SetValue(AUX_GAIN_PTR, -12.5), d.SetValue(DH_FREQ_PTR, 4100.0),
        d.SetValue(DH_THRESH_PTR, -6.0), d.SetValue(LOUD_REF_PTR, -30.0),
        d.SetValue(LOUD_BOOST_PTR, 12.0)}
    off, off_moved = State(profile="m4"), State(profile="m4", **FEATURE_VALUES)
    assert d.diff_ops(off, off_moved) == [] and d.smooth_ops(off, off_moved) == []
    patch = d.ops_to_patch(d.diff_ops(on, moved))
    assert patch["processors"] == {"dh_comp": {"parameters": {"threshold": -6.0}}}
    assert patch["filters"]["sub_loudness"]["parameters"] == {"reference_level": -30.0, "low_boost": 12.0}
    # clamped like every other value
    wild = on.copy()
    wild.aux_gain_db, wild.deharsh_hz, wild.deharsh_thresh_db = 99.0, 100.0, -99.0
    wild.loud_boost_db, wild.loud_ref_db = 40.0, 10.0
    pm = d.param_map(wild)
    assert (pm[AUX_GAIN_PTR], pm[DH_FREQ_PTR], pm[DH_THRESH_PTR], pm[LOUD_BOOST_PTR], pm[LOUD_REF_PTR]) \
        == (12.0, 1500.0, -48.0, 12.0, 0.0)


def _plan_values(plan, ptr):
    return [op.value for _w, b in plan for op in b if isinstance(op, d.SetValue) and op.pointer == ptr]


def test_feature_values_move_click_safe():
    """AUX level and loudness boost raise the PA like a trim: 0.1 dB steps. The loudness
    reference and the de-harsh threshold move like a Biquad gain (0.5 dB), the de-harsh
    frequency in 1/12 octave. Nothing here is guarded (no delay, no polarity)."""
    on = State(profile="m4", aux_mode="mic", loud_on=True, deharsh_on=True)
    for field, ptr, start, end, steps in (
            ("aux_gain_db", AUX_GAIN_PTR, 0.0, 6.0, 60),
            ("loud_boost_db", LOUD_BOOST_PTR, 6.0, 12.0, 60),
            ("loud_ref_db", LOUD_REF_PTR, -10.0, -20.0, 20),
            ("deharsh_thresh_db", DH_THRESH_PTR, -24.0, -12.0, 24),
            ("deharsh_hz", DH_FREQ_PTR, 2700.0, 5400.0, 12)):
        b = on.copy()
        setattr(b, field, end)
        plan = d.smooth_ops(on, b)
        vals = _plan_values(plan, ptr)
        assert len(vals) == steps and vals[-1] == end, field
        assert not any(isinstance(o, d.FaderMute) for _w, bt in plan for o in bt), field
        prev = start
        for v in vals:
            if field == "deharsh_hz":
                assert abs(math.log2(v / prev)) <= 1 / 12 + 1e-3
            else:
                assert abs(v - prev) <= (0.1 if steps == 60 else 0.5) + 1e-9, field
            prev = v
    assert d._is_trim(AUX_GAIN_PTR) and d._is_trim(LOUD_BOOST_PTR) and d._is_trim("/filters/in_trim/parameters/gain")
    assert not d._is_trim(LOUD_REF_PTR) and not d._is_trim(DH_THRESH_PTR)
    assert all(d._guard_group(p) is None for p in FEATURE_PTRS)


# ----------------------------------------------------------------- adoption
def test_feature_values_edited_in_camillagui_are_adopted_clamped():
    base = State(profile="m4", aux_mode="mic", loud_on=True, deharsh_on=True)
    cfg = d.generate(m4(), base)
    cfg["filters"]["aux_gain"]["parameters"].update(gain=0.5, scale="linear")          # -6.02 dB
    cfg["filters"]["sub_loudness"]["parameters"].update(reference_level=-15.0, low_boost=30.0)
    cfg["filters"]["dh_band"]["parameters"]["freq"] = 3300.0
    cfg["processors"]["dh_comp"]["parameters"]["threshold"] = -60.0
    s = d.state_from_config(json.dumps(cfg), None, base)
    assert math.isclose(s.aux_gain_db, -6.02, abs_tol=0.01)
    assert (s.loud_ref_db, s.loud_boost_db, s.deharsh_hz, s.deharsh_thresh_db) == (-15.0, 12.0, 3300.0, -48.0)
    # the clamped values are pushed back by config_drift (the scale change carries its gain)
    drift = d.config_drift(json.dumps(cfg), d.generate(m4(), s))
    assert drift.rebuild == [] and drift.devices == []
    assert set(drift.fixes) == {d.SetValue(AUX_GAIN_PTR, s.aux_gain_db),
                                d.SetValue("/filters/aux_gain/parameters/scale", "dB"),
                                d.SetValue("/filters/aux_gain/parameters/inverted", False),
                                d.SetValue("/filters/aux_gain/parameters/mute", False),
                                d.SetValue(LOUD_BOOST_PTR, 12.0), d.SetValue(DH_THRESH_PTR, -48.0)}
    # a value K1000 wrote itself moments ago is skipped (read-after-write race), processor too
    s2 = d.state_from_config(cfg, None, base, skip_pointers=[DH_THRESH_PTR, LOUD_REF_PTR])
    assert (s2.deharsh_thresh_db, s2.loud_ref_db) == (base.deharsh_thresh_db, base.loud_ref_db)
    assert cfg["processors"]["dh_comp"]["parameters"]["threshold"] == -60.0     # caller's dict kept
    fixes = d.config_drift(cfg, d.generate(m4(), base), skip_pointers=[DH_THRESH_PTR]).fixes
    assert fixes and DH_THRESH_PTR not in {f.pointer for f in fixes}


def test_feature_switches_are_never_adopted_from_the_structure():
    """K1000's state owns the switches: blocks found in the engine do not switch a feature on,
    blocks missing do not switch it off (the drift check restores the structure instead)."""
    on = State(profile="m4", aux_mode="line", loud_on=True, deharsh_on=True, **FEATURE_VALUES)
    off = State(profile="m4")
    s = d.state_from_config(d.generate(m4(), on), None, off)
    assert (s.aux_mode, s.loud_on, s.deharsh_on) == ("off", False, False)
    assert (s.aux_gain_db, s.deharsh_hz, s.loud_ref_db) == (-12.5, 4100.0, -30.0)     # values adopted
    s = d.state_from_config(d.generate(m4(), off), None, on)
    assert s == on.clamped()
    drift = d.config_drift(d.generate(m4(), off), d.generate(m4(), on))
    text = " | ".join(drift.rebuild)
    for needle in ("mixer input", "mixer aux_mix", "mixer dh_split", "mixer dh_sum", "pipeline",
                   "filter aux_gain missing", "filter dh_band missing", "filter sub_loudness missing",
                   "processor dh_comp missing"):
        assert needle in text, needle
    assert drift.fixes == []


# ----------------------------------------------------------------- drift
def test_feature_drift_with_the_engines_json_shapes():
    """GetConfigJson writes every optional key (null when unset) and `processors: null` when
    there are none; neither is a difference."""
    for s in (State(profile="m4"), State(profile="m4", aux_mode="mic", loud_on=True, deharsh_on=True)):
        exp = d.generate(m4(), s)
        live = json.loads(json.dumps(exp))
        live.setdefault("processors", None)
        if s.deharsh_on:
            live["processors"]["dh_comp"]["parameters"].update(soft_clip=None, clip_limit=None)
            live["filters"]["sub_loudness"]["parameters"].update(high_freq=None, low_q=None, high_q=None)
        assert d.config_drift(live, exp).clean
        assert d.state_from_config(live, None, s) == s.clamped()


def test_feature_drift_detects_processor_and_new_mixer_changes():
    exp = d.generate(m4(), State(profile="m4", aux_mode="line", deharsh_on=True, loud_on=True))
    live = json.loads(json.dumps(exp))
    live["processors"]["dh_comp"]["parameters"]["threshold"] = -30.0
    live["processors"]["dh_comp"]["parameters"]["factor"] = 10.0
    live["filters"]["sub_loudness"]["parameters"]["high_boost"] = 6.0
    drift = d.config_drift(live, exp)
    assert drift.devices == [] and drift.rebuild == []
    assert set(drift.fixes) == {d.SetValue(DH_THRESH_PTR, -24.0),
                                d.SetValue("/processors/dh_comp/parameters/factor", 3.0),
                                d.SetValue("/filters/sub_loudness/parameters/high_boost", 0.0)}
    # a time-unit change rewrites the whole processor in one patch (value and unit together)
    live = json.loads(json.dumps(exp))
    live["processors"]["dh_comp"]["parameters"].update(attack=1000.0, attack_unit="us")
    drift = d.config_drift(live, exp)
    assert drift.rebuild == [] and {f.pointer.rsplit("/", 1)[-1] for f in drift.fixes} == set(
        exp["processors"]["dh_comp"]["parameters"])
    assert d.ops_to_patch(drift.fixes)["processors"]["dh_comp"]["parameters"]["attack_unit"] == "ms"
    # structure: a new mixer edited, the processor's type, its channels key gone
    for mutate, needle in (
            (lambda c: c["mixers"]["aux_mix"]["mapping"][0]["sources"].pop(), "mixer aux_mix differs"),
            (lambda c: c["mixers"]["dh_sum"]["mapping"][1]["sources"][1].update(inverted=False),
             "mixer dh_sum differs"),
            (lambda c: c["processors"]["dh_comp"].update(type="NoiseGate"), "processor dh_comp type"),
            (lambda c: c["processors"]["dh_comp"]["parameters"].pop("channels"),
             "processor dh_comp.channels missing"),
            (lambda c: c.update(processors=None), "processor dh_comp missing"),
            (lambda c: c["filters"]["dh_band"]["parameters"].update(type="Peaking"), "dh_band subtype")):
        live = json.loads(json.dumps(exp))
        mutate(live)
        drift = d.config_drift(live, exp)
        assert any(needle in r for r in drift.rebuild), (needle, drift.rebuild)
        assert drift.fixes == []
    # an extra processor the pipeline does not use is not K1000's business
    live = json.loads(json.dumps(d.generate(m4(), State(profile="m4"))))
    live["processors"] = {"spare": exp["processors"]["dh_comp"]}
    assert d.config_drift(live, d.generate(m4(), State(profile="m4"))).clean


def test_aux_routing_cuts_are_held_not_rebuilt():
    """A cut made in CamillaGUI on the AUX routing is the operator's (a ringing mic muted where
    CamillaGUI shows it going into L/R): `config_drift` holds it instead of a rebuild (which put
    the mic back up, and, once the restore kept the cut, rebuilt again every interval; D-016
    review). Only cuts, only on the AUX routing; in the input mixer a lower gain only (a muted
    source leaves a capture channel unused by the first mixer: the engine panics)."""
    exp = d.generate(m4(), State(profile="m4", aux_mode="mic"))

    def drift_of(mutate):
        live = json.loads(json.dumps(exp))
        for e in live["mixers"]["aux_mix"]["mapping"] + live["mixers"]["input"]["mapping"]:
            for x in e["sources"]:
                x.setdefault("mute", False)                       # the engine's JSON shape
        mutate(live["mixers"])
        return d.config_drift(live, exp), live

    def src(mixers, name, dest, ch):
        return next(x for e in mixers[name]["mapping"] if e["dest"] == dest
                    for x in e["sources"] if x["channel"] == ch)

    dr, _ = drift_of(lambda m: [src(m, "aux_mix", k, 2).update(mute=True) for k in (0, 1)])
    assert (dr.rebuild, dr.fixes, dr.held) == ([], [], ("aux_mix 2->0 MUTED", "aux_mix 2->1 MUTED"))
    assert not dr.clean
    dr, _ = drift_of(lambda m: src(m, "aux_mix", 0, 2).update(gain=-30.0))
    assert dr.rebuild == [] and dr.held == ("aux_mix 2->0 -30.0 dB",)
    dr, _ = drift_of(lambda m: m["aux_mix"]["mapping"][1].update(mute=True))   # R muted whole
    assert dr.rebuild == [] and dr.held == ("aux_mix ->1 MUTED",)
    dr, _ = drift_of(lambda m: src(m, "input", 2, 0).update(gain=-20.0))       # the M4's IN1
    assert dr.rebuild == [] and dr.held == ("input 0->2 -20.0 dB",)
    for mutate in (lambda m: src(m, "aux_mix", 0, 2).update(gain=0.0),        # a raise
                   lambda m: src(m, "aux_mix", 0, 2).update(inverted=True),
                   lambda m: src(m, "aux_mix", 0, 2).update(gain=0.5, scale="linear"),
                   lambda m: src(m, "input", 2, 0).update(mute=True),          # engine panic
                   lambda m: src(m, "input", 0, 2).update(gain=-6.0),          # the program
                   lambda m: m["aux_mix"]["mapping"][0]["sources"].pop(),
                   lambda m: src(m, "split", 0, 0).update(mute=True)):         # not AUX routing
        dr, _ = drift_of(mutate)
        assert dr.rebuild and dr.held == (), dr
    # features off: the input mixer is f2671b0's, any change rebuilds as before
    off = d.generate(m4(), State(profile="m4"))
    live = json.loads(json.dumps(off))
    live["mixers"]["input"]["mapping"][0]["sources"][0]["gain"] = -6.0
    assert d.config_drift(live, off).rebuild == ["mixer input differs"]
    # a restore carries the cuts onto K1000's routing, even where the rest of the mixer differs
    _, live = drift_of(lambda m: (src(m, "aux_mix", 0, 2).update(mute=True),
                                  src(m, "aux_mix", 1, 2).update(gain=-30.0),
                                  src(m, "input", 2, 0).update(gain=-20.0, mute=True),
                                  m["aux_mix"]["mapping"][0]["sources"].append(
                                      {"channel": 3, "gain": 0.0, "inverted": False})))
    out = copy.deepcopy(exp["mixers"])
    for name in out:
        d.carry_mixer_cuts(name, live["mixers"].get(name), out[name])
    assert src(out, "aux_mix", 0, 2)["mute"] is True and src(out, "aux_mix", 1, 2)["gain"] == -30.0
    assert src(out, "input", 2, 0)["gain"] == -20.0 and "mute" not in src(out, "input", 2, 0)
    assert len(out["aux_mix"]["mapping"][0]["sources"]) == 2           # K1000's routing
    assert exp["mixers"]["aux_mix"]["mapping"][0]["sources"][1].get("mute") is None  # not mutated
    restored = copy.deepcopy(exp)
    restored["mixers"] = out
    dr = d.config_drift(restored, exp)
    assert dr.rebuild == [] and set(dr.held) == {
        "aux_mix 2->0 MUTED", "aux_mix 2->1 -30.0 dB", "input 0->2 -20.0 dB"}


def test_signal_generator_variant_keeps_feature_channels():
    for prof in (m4(), m4(capture_channels=6)):
        cfg = d.generate(prof, State(profile="m4", aux_mode="line", deharsh_on=True, loud_on=True))
        v = d.signal_generator_variant(cfg)
        _channel_flow(v)
        assert v["devices"]["capture"]["channels"] == prof.capture_channels
        assert v["processors"] == cfg["processors"] and v["mixers"] == cfg["mixers"]


# ============================================================================ 2026-10-16 features (engine)
def _feature_validity_cases():
    cases = [(pk, sk) for pk in ("dev", "m4") for sk in feature_states()]
    cases += [(pk, sk) for pk in ("m4_6ch", "m4_in12", "m4_3ch")
              for sk in ("aux_mic_loud_dh", "aux_line_loud_dh_mono")]
    return cases


@engine
@needs_cdsp
@pytest.mark.parametrize("prof_key,state_key", _feature_validity_cases())
def test_camilladsp_accepts_every_feature_combination(tmp_path, prof_key, state_key):
    prof = feature_profiles()[prof_key]
    s = feature_states(prof.name)[state_key]
    path = tmp_path / f"{prof_key}_{state_key}.yml"
    path.write_text(d.to_yaml(d.generate(prof, s)))
    r = subprocess.run([CDSP, "-c", str(path)], capture_output=True, text=True, timeout=30)
    assert "Config is valid" in r.stdout + r.stderr, r.stdout + r.stderr
    assert r.returncode == 0


SR = 48000


def _offline(tmp_path, cfg, x, main_db=0.0, tag="run"):
    """Play the capture samples `x` (frames x channels) through `cfg` in camilladsp, offline:
    RawFile capture -> File playback, both F64 (no ALSA, no websocket), Main at `main_db`.
    Returns the played frames (frames x playback channels), sample-aligned with `x` (the
    pipeline is IIR only: no latency)."""
    np = pytest.importorskip("numpy")
    if CDSP is None:
        pytest.skip("camilladsp binary not installed")
    cfg = copy.deepcopy(cfg)
    src, out, conf = (tmp_path / f"{tag}.in.raw", tmp_path / f"{tag}.out.raw", tmp_path / f"{tag}.yml")
    np.ascontiguousarray(x, dtype="<f8").tofile(src)
    n_out = cfg["devices"]["playback"]["channels"]
    cfg["devices"]["capture"] = {"type": "RawFile", "channels": int(x.shape[1]), "filename": str(src),
                                 "format": "F64_LE"}
    cfg["devices"]["playback"] = {"type": "File", "channels": n_out, "filename": str(out),
                                  "format": "F64_LE"}
    conf.write_text(d.to_yaml(cfg))
    r = subprocess.run([CDSP, f"-g{main_db:g}", str(conf)], capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stdout[-1000:] + r.stderr[-2000:]
    y = np.fromfile(out, dtype="<f8").reshape(-1, n_out)
    assert y.shape[0] == x.shape[0]
    return y


def _tone_db(y, freq, ref=1.0):
    """Level of the `freq` component over the last second (dB re a `ref` peak amplitude): one
    exact DFT bin (whole cycles per second at 48 kHz), so the other test tones do not leak."""
    import numpy as np
    seg = np.asarray(y)[-SR:]
    k = np.arange(len(seg))
    m = 2.0 / len(seg) * abs(np.sum(seg * np.exp(-2j * np.pi * freq * k / SR)))
    return 20.0 * math.log10(m / ref) if m > 0 else -math.inf


def _sine(freq, amp, n=2 * SR):
    import numpy as np
    return amp * np.sin(2 * np.pi * freq * np.arange(n) / SR)


def _biquad_db(b, a, freq):
    import numpy as np
    z = np.exp(-1j * 2 * math.pi * freq / SR)
    return 20 * math.log10(abs((b[0] + b[1] * z + b[2] * z * z) / (a[0] + a[1] * z + a[2] * z * z)))


def _highpass_db(freq, f0, q):
    """The engine's Biquad Highpass (RBJ cookbook, camilladsp-schema biquad.rs) at `freq`."""
    w = 2 * math.pi * f0 / SR
    sn, cs = math.sin(w), math.cos(w)
    al = sn / (2 * q)
    return _biquad_db([(1 + cs) / 2, -(1 + cs), (1 + cs) / 2], [1 + al, -2 * cs, 1 - al], freq)


def _lowshelf_db(freq, f0, q, gain):
    """The engine's Lowshelf with Q (what the Loudness filter builds) at `freq`."""
    w = 2 * math.pi * f0 / SR
    sn, cs = math.sin(w), math.cos(w)
    A = 10 ** (gain / 40)
    be = sn * math.sqrt(A) / q
    return _biquad_db([A * ((A + 1) - (A - 1) * cs + be), 2 * A * ((A - 1) - (A + 1) * cs),
                       A * ((A + 1) - (A - 1) * cs - be)],
                      [(A + 1) + (A - 1) * cs + be, -2 * ((A - 1) + (A + 1) * cs),
                       (A + 1) + (A - 1) * cs - be], freq)


def _deharsh_model(xl, xr, freq, q, thresh, ratio, attack_ms, release_ms):
    """De-harsh as the engine computes it: RBJ band-pass (0 dB at centre), the compressor of
    compressor.rs (level = 20 log10(|band L + band R| + 1e-9) per sample, one-pole attack/release
    in dB from -100, gain -(level - thresh)(ratio - 1)/ratio above thresh), then x - B + g B."""
    import numpy as np
    w = 2 * math.pi * freq / SR
    al = math.sin(w) / (2 * q)
    a0 = 1 + al
    b0, b2, a1, a2 = al / a0, -al / a0, -2 * math.cos(w) / a0, (1 - al) / a0

    def bp(x):
        y = np.zeros_like(x)
        s1 = s2 = 0.0
        for i, v in enumerate(x):
            o = b0 * v + s1
            s1 = -a1 * o + s2
            s2 = b2 * v - a2 * o
            y[i] = o
        return y

    bl, br = bp(xl), bp(xr)
    att, rel = math.exp(-1 / (attack_ms * SR / 1000)), math.exp(-1 / (release_ms * SR / 1000))
    prev, g = -100.0, np.empty_like(bl)
    for i, v in enumerate(bl + br):
        lv = 20 * math.log10(abs(v) + 1e-9)
        lv = att * prev + (1 - att) * lv if lv >= prev else rel * prev + (1 - rel) * lv
        prev = lv
        g[i] = 10 ** ((-(lv - thresh) * (ratio - 1) / ratio if lv > thresh else 0.0) / 20)
    return xl - bl + g * bl, xr - br + g * br


def _program(*cols, n=2 * SR, channels=4):
    """Capture frames: the given columns {channel: samples}, silence elsewhere."""
    import numpy as np
    x = np.zeros((n, channels))
    for ch, sig in cols:
        x[:, ch] = sig
    return x


@engine
@needs_cdsp
@pytest.mark.parametrize("prof_key", ["m4", "dev"])
def test_engine_features_off_plays_exactly_like_f2671b0(tmp_path, prof_key):
    """The HEAD config (golden YAML, tuned show state) and today's features-off config, with the
    features' values moved, give the same samples on every output, bit for bit."""
    np = pytest.importorskip("numpy")
    name, ov = golden.PROFILES[prof_key]
    prof = pf.load_profile(name, ov or None, probe=False)
    head = yaml.safe_load(golden.golden_path(prof_key, "tuned").read_text())
    s = golden.STATES["tuned"](State)
    for k, v in FEATURE_VALUES.items():
        setattr(s, k, v)
    rng = np.random.default_rng(7)
    n = 2 * SR
    x = 0.05 * rng.standard_normal((n, prof.capture_channels))
    for ch, f in zip(range(prof.capture_channels), (55.0, 440.0, 2700.0, 6100.0, 900.0, 1200.0)):
        x[:, ch] += _sine(f, 0.2, n)
    y_head = _offline(tmp_path, head, x, -6.0, "head")
    y_now = _offline(tmp_path, d.generate(prof, s), x, -6.0, "now")
    assert np.array_equal(y_head, y_now)
    assert np.max(np.abs(y_head)) > 0.05                       # it played something


@engine
@needs_cdsp
def test_engine_deharsh_with_the_threshold_parked_is_a_pass_through(tmp_path):
    """Under the threshold the compressor's gain is exactly 1: L - B + B = L (to rounding)."""
    np = pytest.importorskip("numpy")
    rng = np.random.default_rng(3)
    n = 2 * SR
    x = _program((2, _sine(2700.0, 0.25) + 0.05 * rng.standard_normal(n)),
                 (3, _sine(3100.0, 0.25) + 0.05 * rng.standard_normal(n)))
    off = _offline(tmp_path, d.generate(m4(), State(profile="m4")), x, 0.0, "off")
    on = _offline(tmp_path, d.generate(m4(), State(profile="m4", deharsh_on=True,
                                                   deharsh_thresh_db=RANGES["deharsh_thresh_db"].max)),
                  x, 0.0, "parked")
    for ch in range(4):
        rms = math.sqrt(float(np.mean(off[:, ch] ** 2)))
        err = float(np.max(np.abs(on[:, ch] - off[:, ch])))
        assert rms > 0.05 and (err == 0.0 or 20 * math.log10(err / rms) < -120.0), (ch, err, rms)


@engine
@needs_cdsp
def test_engine_deharsh_cuts_a_loud_band_by_its_gain_reduction_and_leaves_500_hz(tmp_path):
    """L = R = a 2.7 kHz and a 500 Hz tone, both at -12 dBFS peak. The band copies sum to a
    -6 dBFS peak at the compressor (correlated L/R: +6 dB), well over the -24 dB threshold: the
    2.7 kHz tone is cut by the compressor's gain reduction (here ~12 dB, the 3:1 slope of the
    smoothed level's excess), the 500 Hz tone keeps its level (within 0.5 dB). The cut matches a
    model of the engine's band-pass + compressor to 0.25 dB."""
    pytest.importorskip("numpy")
    tone = _sine(2700.0, 0.25) + _sine(500.0, 0.25)
    x = _program((2, tone), (3, tone))
    s = State(profile="m4", deharsh_on=True)
    off = _offline(tmp_path, d.generate(m4(), State(profile="m4")), x, 0.0, "off")
    on = _offline(tmp_path, d.generate(m4(), s), x, 0.0, "on")
    ml, _mr = _deharsh_model(tone, tone, s.deharsh_hz, d.DEHARSH_Q, s.deharsh_thresh_db,
                             d.DEHARSH_RATIO, d.DEHARSH_ATTACK_MS, d.DEHARSH_RELEASE_MS)
    model_cut = _tone_db(ml, 2700.0) - _tone_db(tone, 2700.0)
    assert -16.0 < model_cut < -8.0, model_cut              # a real gain reduction, not a rounding
    for ch in range(4):                                      # Top L/R, Sub L/R
        cut = _tone_db(on[:, ch], 2700.0) - _tone_db(off[:, ch], 2700.0)
        assert abs(cut - model_cut) < 0.25, (ch, cut, model_cut)
        assert abs(_tone_db(on[:, ch], 500.0) - _tone_db(off[:, ch], 500.0)) < 0.5, ch
    # the 500 Hz tone alone, same level: untouched
    x5 = _program((2, _sine(500.0, 0.25)), (3, _sine(500.0, 0.25)))
    off5 = _offline(tmp_path, d.generate(m4(), State(profile="m4")), x5, 0.0, "off5")
    on5 = _offline(tmp_path, d.generate(m4(), s), x5, 0.0, "on5")
    for ch in range(4):
        assert abs(_tone_db(on5[:, ch], 500.0) - _tone_db(off5[:, ch], 500.0)) < 0.5, ch


@engine
@needs_cdsp
def test_engine_sub_loudness_follows_main_under_the_reference(tmp_path):
    """reference -10 dB, boost 6 dB. Main at (or above) the reference: the subs are bit for bit
    loudness-off's. Main 10 dB under it: the subs' 70 Hz shelf gets low_boost/2 = 3 dB, i.e. the
    engine's Lowshelf(70 Hz, Q 0.7071, +3 dB) at 40 Hz (2.71 dB), nothing at 1 kHz, the tops
    untouched; and the subs are still quieter than at the reference."""
    np = pytest.importorskip("numpy")
    tone = _sine(40.0, 0.1) + _sine(1000.0, 0.1)
    x = _program((2, tone), (3, tone))
    loud = State(profile="m4", loud_on=True, loud_ref_db=-10.0, loud_boost_db=6.0)
    plain = State(profile="m4")
    sub40 = {}
    for main in (-10.0, -5.0, -20.0):
        on = _offline(tmp_path, d.generate(m4(), loud), x, main, f"on{main:g}")
        off = _offline(tmp_path, d.generate(m4(), plain), x, main, f"off{main:g}")
        assert np.array_equal(on[:, :2], off[:, :2]), main            # physical 1-2: the tops
        sub40[main] = _tone_db(on[:, 2], 40.0)
        if main >= loud.loud_ref_db:
            assert np.array_equal(on, off), main
            continue
        boost = loud.loud_boost_db * min((loud.loud_ref_db - main) / 20.0, 1.0)
        want = _lowshelf_db(40.0, d.LOUD_LOW_FREQ_HZ, 1 / math.sqrt(2), boost)
        for ch in (2, 3):                                              # physical 3-4: the subs
            got = _tone_db(on[:, ch], 40.0) - _tone_db(off[:, ch], 40.0)
            assert abs(got - want) < 0.05, (main, ch, got, want)
            assert 0.8 * boost <= got <= boost
            assert abs(_tone_db(on[:, ch], 1000.0) - _tone_db(off[:, ch], 1000.0)) < 0.01
    assert sub40[-20.0] < sub40[-10.0] - 6.0                            # 10 dB down, at most 3 back


@engine
@needs_cdsp
def test_engine_aux_routes_in1_in2_after_the_trim(tmp_path):
    """Per-channel capture (the SignalGenerator puts one signal on every channel): IN1 = 300 Hz +
    30 Hz, IN2 = 700 Hz, LINE IN 3/4 (program) = 1 kHz / 1.5 kHz, all at -20 dBFS peak. Input
    trim -6 dB (program only), AUX -4 dB. Mic: IN1 to both sides at -4 - 3 dB through the 100 Hz
    high-pass (30 Hz ~21 dB further down), IN2 nowhere. Line: IN1 to L, IN2 to R at -4 dB, no
    high-pass. Off: no AUX at all. Measured on the subs (no Lo-Cut, low-pass parked)."""
    pytest.importorskip("numpy")
    amp = 0.1
    x = _program((0, _sine(300.0, amp) + _sine(30.0, amp)), (1, _sine(700.0, amp)),
                 (2, _sine(1000.0, amp)), (3, _sine(1500.0, amp)))
    silent = -120.0
    for mode in ("off", "mic", "line"):
        s = State(profile="m4", aux_mode=mode, aux_gain_db=-4.0, input_trim_db=-6.0, lowcut_on=False)
        y = _offline(tmp_path, d.generate(m4(), s), x, 0.0, mode)
        sub_l, sub_r = y[:, 2], y[:, 3]

        def lvl(sig, f):
            return _tone_db(sig, f, amp)

        assert abs(lvl(sub_l, 1000.0) + 6.0) < 0.02 and abs(lvl(sub_r, 1500.0) + 6.0) < 0.02, mode
        assert lvl(sub_l, 1500.0) < silent and lvl(sub_r, 1000.0) < silent, mode
        if mode == "off":
            for f in (30.0, 300.0, 700.0):
                assert lvl(sub_l, f) < silent and lvl(sub_r, f) < silent, (mode, f)
        elif mode == "mic":
            for sig in (sub_l, sub_r):
                for f in (300.0, 30.0):
                    want = -4.0 - 3.0 + _highpass_db(f, d.AUX_MIC_HP_HZ, d.AUX_MIC_HP_Q)
                    assert abs(lvl(sig, f) - want) < 0.02, (mode, f, lvl(sig, f), want)
                assert lvl(sig, 700.0) < silent
            assert lvl(sub_l, 300.0) - lvl(sub_l, 30.0) > 20.0              # the high-pass works
        else:
            assert abs(lvl(sub_l, 300.0) + 4.0) < 0.02 and abs(lvl(sub_l, 30.0) + 4.0) < 0.02
            assert abs(lvl(sub_r, 700.0) + 4.0) < 0.02
            assert lvl(sub_l, 700.0) < silent and lvl(sub_r, 300.0) < silent and lvl(sub_r, 30.0) < silent


@engine
@needs_cdsp
def test_engine_aux_on_dev_plays_like_aux_off(tmp_path):
    np = pytest.importorskip("numpy")
    x = _program((0, _sine(440.0, 0.2)), (1, _sine(880.0, 0.2)), channels=2)
    off = _offline(tmp_path, d.generate(dev(), State()), x, 0.0, "off")
    for mode in ("mic", "line"):
        assert np.array_equal(_offline(tmp_path, d.generate(dev(), State(aux_mode=mode)), x, 0.0, mode), off)


def _rms_since(cmd, seconds=0.3):
    return cmd("GetPlaybackSignalRmsSince", value=seconds)["value"]


def _settled_rms(cmd, ch, wait=0.6):
    time.sleep(wait)
    return _rms_since(cmd)[ch]


@engine
def test_engine_feature_values_apply_live_without_a_rebuild(tmp_path):
    """Each owned pointer of the three blocks, written by SetConfigValue on a running engine
    (SignalGenerator -> /dev/null): accepted, read back, audible on the playback RMS, and only
    "Updating parameters" in the log (no pipeline rebuild, no device restart)."""
    def check_log(log):
        text = log.read_text()
        assert "Rebuilding pipeline" not in text and "Devices changed" not in text
        assert "Updating parameters" in text

    def write(cmd, ptr, value):
        assert cmd("SetConfigValue", pointer=ptr, value=value)["result"] == "Ok", ptr
        assert _eventually(cmd, ptr, value) == value, ptr

    # AUX: the generator's sine is on every capture channel, so line AUX doubles Top L (+6 dB);
    # at -40 dB it adds ~0.09 dB
    s = State(profile="m4", aux_mode="line", loud_on=True, deharsh_on=True, deharsh_thresh_db=0.0)
    cfg = d.signal_generator_variant(d.generate(m4(), s), level_db=-30.0, freq=1000.0)
    with _engine(tmp_path, cfg, "-g0") as (cmd, log):
        r0 = _settled_rms(cmd, 0, 1.0)
        write(cmd, AUX_GAIN_PTR, -40.0)
        r1 = _settled_rms(cmd, 0)
        want = 20 * math.log10(2.0) - 20 * math.log10(1.0 + 10 ** (-40 / 20))
        assert abs((r0 - r1) - want) < 0.3, (r0, r1, want)
        # a whole state move as one PatchConfig, then adoption and drift on the live JSON
        s2 = s.copy()
        for k, v in FEATURE_VALUES.items():
            setattr(s2, k, v)
        s1 = s.copy()
        s1.aux_gain_db = -40.0
        ops = d.diff_ops(s1, s2)
        assert ops and all(isinstance(o, d.SetValue) for o in ops)
        assert cmd("PatchConfig", value=d.ops_to_patch(ops))["result"] == "Ok"
        for op in ops:
            assert _eventually(cmd, op.pointer, op.value) == op.value, op
        live = cmd("GetConfigJson")["value"]
        assert json.loads(live).get("processors", {}).get("dh_comp")
        assert d.state_from_config(live, None, s) == s2.clamped()
        assert d.config_drift(live, d.signal_generator_variant(d.generate(m4(), s2))).clean
        assert cmd("GetState")["value"] == "Running"
        check_log(log)

    # de-harsh: a loud 2.7 kHz tone; threshold down -> cut; band moved off the tone -> cut gone
    s = State(profile="m4", deharsh_on=True, deharsh_thresh_db=0.0, loud_on=True)
    cfg = d.signal_generator_variant(d.generate(m4(), s), level_db=-12.0, freq=2700.0)
    with _engine(tmp_path, cfg, "-g0") as (cmd, log):
        r0 = _settled_rms(cmd, 0, 1.0)
        write(cmd, DH_THRESH_PTR, -40.0)
        r1 = _settled_rms(cmd, 0)
        assert r1 < r0 - 10.0, (r0, r1)
        write(cmd, DH_FREQ_PTR, 6000.0)
        r2 = _settled_rms(cmd, 0)
        assert r2 > r1 + 6.0, (r1, r2)
        assert cmd("PatchConfig", value=d.ops_to_patch([d.SetValue(DH_THRESH_PTR, 0.0),
                                                        d.SetValue(DH_FREQ_PTR, 2700.0)]))["result"] == "Ok"
        assert abs(_settled_rms(cmd, 0) - r0) < 0.1
        assert cmd("GetState")["value"] == "Running"
        check_log(log)

    # loudness: 40 Hz, Main at -30 = 20 dB under the -10 reference -> the full shelf on the subs
    s = State(profile="m4", loud_on=True, loud_ref_db=-10.0, loud_boost_db=6.0, deharsh_on=True,
              deharsh_thresh_db=0.0)
    cfg = d.signal_generator_variant(d.generate(m4(), s), level_db=-20.0, freq=40.0)
    full = _lowshelf_db(40.0, d.LOUD_LOW_FREQ_HZ, 1 / math.sqrt(2), 6.0)
    with _engine(tmp_path, cfg, "-g-30") as (cmd, log):
        r0 = _settled_rms(cmd, 2, 1.0)                                    # physical 3 = Sub L
        write(cmd, LOUD_BOOST_PTR, 0.0)
        r1 = _settled_rms(cmd, 2)
        assert abs((r0 - r1) - full) < 0.3, (r0, r1, full)
        write(cmd, LOUD_BOOST_PTR, 6.0)
        assert abs(_settled_rms(cmd, 2) - r0) < 0.1
        write(cmd, LOUD_REF_PTR, -30.0)                                   # Main = reference: nothing
        assert abs(_settled_rms(cmd, 2) - r1) < 0.1
        assert cmd("GetState")["value"] == "Running"
        check_log(log)


@engine
def test_engine_feature_switches_rebuild_live(tmp_path):
    """Switching the blocks is a whole-config upload: accepted on a running engine, a pipeline
    rebuild only (no device restart), and the running config then matches K1000's exactly."""
    off = State(profile="m4")
    on = State(profile="m4", aux_mode="line", loud_on=True, deharsh_on=True)
    with _engine(tmp_path, d.signal_generator_variant(d.generate(m4(), off)), "-g-40") as (cmd, log):
        for s in (on, State(profile="m4", aux_mode="mic", deharsh_on=True), off):
            text0 = log.read_text()
            exp = d.signal_generator_variant(d.generate(m4(), s))
            assert cmd("SetConfigJson", value=json.dumps(exp))["result"] == "Ok"
            time.sleep(0.5)
            new = log.read_text()[len(text0):]
            assert "Rebuilding pipeline" in new and "Devices changed" not in new
            assert cmd("GetState")["value"] == "Running"
            live = cmd("GetConfigJson")["value"]
            assert d.config_drift(live, exp).clean
            assert d.state_from_config(live, None, s) == s.clamped()


@engine
@needs_cdsp
def test_engine_client_confirms_writes_that_f32_cannot_hold(tmp_path):
    """CamillaDSP 5 keeps the Loudness filter's reference_level, low_boost and high_boost as f32
    (camilladsp-schema LoudnessParameters: FiniteF32), and GetConfigValue returns them widened to
    f64: a write of 6.1 reads back 6.099999904632568. `CamillaClient.set_value` confirms every
    write with that read-back, so it must take the f32 value as the one written. Before it did
    (review 2026-10-16), every loudness boost detent (0.1 dB steps: 6.1, 6.2...) and every drift
    push-back of the treble boost failed as NotApplied after the 0.5 s window, behind the client
    lock (a B4 mute waited). Every owned D-016 pointer, at a value off the f32 grid, through the
    real client: confirmed at once, and read back as written (within f32)."""
    pytest.importorskip("websockets")
    import asyncio
    from k1000.cdsp import CamillaClient
    s = State(profile="m4", aux_mode="mic", loud_on=True, deharsh_on=True)
    cfg = d.signal_generator_variant(d.generate(m4(), s), level_db=-30.0)
    writes = [(LOUD_BOOST_PTR, 6.1), (LOUD_BOOST_PTR, 6.2), (LOUD_REF_PTR, -10.3),
              ("/filters/sub_loudness/parameters/high_boost", 0.7), (AUX_GAIN_PTR, -3.1),
              (DH_THRESH_PTR, -24.3), (DH_FREQ_PTR, 2716.77)]

    async def body():
        c = CamillaClient(f"ws://127.0.0.1:{PORT}", backoff_min=0.05, backoff_max=0.2)
        assert await c.connect(timeout=5)
        try:
            for ptr, v in writes:
                t = time.monotonic()
                assert await c.set_value(ptr, v) == v, ptr          # no CdspNotApplied
                assert time.monotonic() - t < c.confirm_timeout, ptr
                assert math.isclose(await c.get_value(ptr), v, rel_tol=1e-6), ptr
            # the PatchConfig path (GetConfigJson read-back) still confirms too
            await c.patch_config(d.ops_to_patch([d.SetValue(LOUD_BOOST_PTR, 6.3),
                                                 d.SetValue(LOUD_REF_PTR, -10.7)]))
        finally:
            await c.close()

    with _engine(tmp_path, cfg, "-g-20") as (cmd, log):
        asyncio.run(asyncio.wait_for(body(), 30))
        assert cmd("GetState")["value"] == "Running"
        live = json.loads(cmd("GetConfigJson")["value"])
        assert live["filters"]["sub_loudness"]["parameters"]["low_boost"] == 6.3


@engine
@needs_cdsp
def test_engine_aux_mix_cut_from_the_gui_is_held_and_a_restore_keeps_it(tmp_path):
    """On the Pi's engine: CamillaGUI mutes the mic on `aux_mix` and turns IN1 down in the input
    mixer (SetConfigValue on the mixer sources). The running config's JSON then reads as held
    cuts (no rebuild), and a restore carrying them (`carry_mixer_cuts`) is accepted and plays
    the mic muted. D-016 review: the restore used to bring it back at -3 dB."""
    s = State(profile="m4", aux_mode="mic")
    exp = d.signal_generator_variant(d.generate(m4(), s), level_db=-30.0)
    a1 = next(i for i, x in enumerate(exp["mixers"]["aux_mix"]["mapping"][0]["sources"])
              if x["channel"] == 2)
    with _engine(tmp_path, exp, "-g-20") as (cmd, log):
        # one write at a time, each confirmed (the engine's stale-write window, `_read_back`)
        writes = [(f"/mixers/aux_mix/mapping/{dest}/sources/{a1}/mute", True) for dest in (0, 1)]
        for ptr, v in writes + [("/mixers/input/mapping/2/sources/0/gain", -30.0)]:
            assert cmd("SetConfigValue", pointer=ptr, value=v)["result"] == "Ok"
            assert _eventually(cmd, ptr, v) == v, ptr
        live = json.loads(cmd("GetConfigJson")["value"])
        drift = d.config_drift(live, exp)
        assert drift.rebuild == [] and set(drift.held) == {
            "aux_mix 2->0 MUTED", "aux_mix 2->1 MUTED", "input 0->2 -30.0 dB"}, drift
        restored = copy.deepcopy(exp)
        restored["pipeline"] = list(exp["pipeline"])
        for name, m in restored["mixers"].items():
            d.carry_mixer_cuts(name, live["mixers"].get(name), m)
        assert cmd("SetConfigJson", value=json.dumps(restored))["result"] == "Ok"
        time.sleep(0.5)
        assert cmd("GetState")["value"] == "Running"
        live = json.loads(cmd("GetConfigJson")["value"])
        assert live["mixers"]["aux_mix"]["mapping"][0]["sources"][a1]["mute"] is True
        assert d.config_drift(live, exp).rebuild == []
