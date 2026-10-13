# SPDX-License-Identifier: GPL-3.0-or-later
"""Venue snapshots (docs/12-venue-snapshots.md): the store (venues.py), the Controller's save /
recall / undo against the in-memory engine of test_control, the VENUES page's input map, the
screen, and the phone messages (web/server.py) on the bridge and over the websocket.

No pytest-asyncio on the box (apt only): each async test drives its own asyncio.run().
"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import connect as ws_connect

from k1000 import dspconfig as dc
from k1000 import venues as vn
from k1000.control import UNDO_RECALL_S, Controller, Paths
from k1000.state import RANGES, State
from k1000.ui import input_map as im
from k1000.ui import pages
from k1000.ui.input_map import (Focus, RecallVenue, RenameVenue, SaveVenue, ShowHint, UndoRecall,
                                map_event)
from k1000.web.server import PhoneBridge, PhoneServer
from test_control import boot, make, nomark, run, sets_for, until
from test_ui import Enc, Sw
from test_web import recv_until, settings


# ======================================================================= helpers
def venue_state() -> State:
    """A state that differs from the defaults in every venue value (and in what is never stored)."""
    s = State()
    s.sub_delay_ms, s.top_delay_ms = 2.9, 0.5
    s.sub_inverted, s.top_inverted = True, False
    s.lowcut_on, s.lowcut_hz = True, 90.0
    s.balance_db = 2.0
    s.live_peq[0].freq, s.live_peq[0].gain, s.live_peq[0].q = 2500.0, -6.0, 8.0
    s.live_peq[2].gain = -3.0
    s.live_lowshelf.gain = 2.0
    s.sys_peq[1].gain, s.sys_peq[5].gain = -4.0, 2.5
    return s


def write_venues(path: Path, slots: list[Any]) -> None:
    path.write_text(json.dumps({"schema": 1, "slots": slots}), encoding="utf-8")


def slot_dict(state: State, name: str = "Hall", at: float = 1760000000.0) -> dict[str, Any]:
    return {"name": name, "saved_at": at, "values": vn.values_of(state)}


def venue_part(s: State) -> dict[str, Any]:
    return vn.values_of(s)


def never_part(s: State) -> dict[str, Any]:
    d = s.to_dict()
    return {k: d[k] for k in vn.NEVER_KEYS}


# ======================================================================= the store
def test_keys_cover_the_spec_and_never_master_or_mute() -> None:
    """What a slot holds is exactly the spec's list; master, mute, trims, profile, devices never."""
    assert set(vn.VENUE_KEYS) == {"live_peq", "live_lowshelf", "live_highshelf", "sys_peq",
                                  "sub_delay_ms", "top_delay_ms", "sub_inverted", "top_inverted",
                                  "lowcut_on", "lowcut_hz", "balance_db", "mono_subs"}
    assert not set(vn.VENUE_KEYS) & set(vn.NEVER_KEYS)
    assert {"master_db", "muted", "input_trim_db", "out_trim_db", "profile"} <= set(vn.NEVER_KEYS)
    # every State field is either stored or deliberately never stored
    assert set(vn.VENUE_KEYS) | set(vn.NEVER_KEYS) == set(State().to_dict()) - {"schema"}
    v = vn.values_of(venue_state())
    assert len(v["live_peq"]) == 4 and len(v["sys_peq"]) == 8
    assert set(v["live_lowshelf"]) == {"freq", "gain", "q"}


def test_store_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "venues.json"
    st = vn.VenueStore(p)
    assert st.slots == [None] * vn.N_SLOTS and not p.exists()
    a = vn.Venue("HALL", 1760000000.5, vn.values_of(venue_state()))
    b = vn.Venue("GARDEN", 0.0, vn.values_of(State()))
    st.put(0, a)
    st.put(7, b)
    assert st.save()
    data = json.loads(p.read_text())
    assert data["schema"] == vn.SCHEMA_VERSION and len(data["slots"]) == vn.N_SLOTS
    again = vn.VenueStore(p)
    assert again.slots[0] == a and again.slots[7] == b
    assert again.slots[1:7] == [None] * 6
    assert again.find(a.values) == 0 and again.find(vn.values_of(State(balance_db=1.0))) is None
    with pytest.raises(IndexError):
        st.put(8, a)
    assert st.get(8) is None and st.get(-1) is None and st.get("1") is None   # type: ignore[arg-type]


@pytest.mark.parametrize("content", [b"{\"schema\": 1, \"slots\": [", b"\x00\xff\xfe", b"[1, 2, 3]",
                                     b"{\"slots\": \"x\"}", b"", b"[" * 100000])
def test_torn_or_invalid_file_is_empty_slots_never_a_crash(tmp_path: Path, content: bytes,
                                                         caplog: Any) -> None:
    p = tmp_path / "venues.json"
    p.write_bytes(content)
    with caplog.at_level(logging.WARNING, logger="k1000.venues"):
        st = vn.VenueStore(p)
    assert st.slots == [None] * vn.N_SLOTS
    assert st.load_error
    assert any("venues file" in r.getMessage() for r in caplog.records)
    assert (tmp_path / "venues.json.bad").read_bytes() == content     # the evidence survives
    st.put(2, vn.Venue("X", 1.0, vn.values_of(State())))
    assert st.save() and vn.VenueStore(p).slots[2] is not None        # and the next save heals


def test_one_bad_slot_leaves_the_others(tmp_path: Path) -> None:
    p = tmp_path / "venues.json"
    write_venues(p, [slot_dict(venue_state(), "ok"), "junk", {"name": "no values"},
                     {"values": "x"}, None, slot_dict(State(), "five")])
    st = vn.VenueStore(p)
    assert st.slots[0] is not None and st.slots[0].name == "OK"
    assert st.slots[1:5] == [None] * 4
    assert st.slots[5] is not None and st.slots[5].name == "FIVE"
    assert st.load_error == ""


def test_eight_slots_at_most(tmp_path: Path, caplog: Any) -> None:
    p = tmp_path / "venues.json"
    write_venues(p, [slot_dict(State(balance_db=float(i)), f"v{i}") for i in range(12)])
    with caplog.at_level(logging.WARNING, logger="k1000.venues"):
        st = vn.VenueStore(p)
    assert len(st.slots) == vn.N_SLOTS
    assert [v.name for v in st.slots if v] == [f"V{i}" for i in range(8)]
    assert any("only the first 8" in r.getMessage() for r in caplog.records)
    st.save()
    assert len(json.loads(p.read_text())["slots"]) == vn.N_SLOTS


def test_values_are_clamped_like_state_json(tmp_path: Path) -> None:
    """Out of range -> the edge, garbage -> the default, missing -> the default: State.from_dict."""
    raw = vn.values_of(State())
    raw.update(sub_delay_ms=999.0, top_delay_ms=-5, balance_db="loud", lowcut_hz=1e9,
               sub_inverted="yes", mono_subs=1)
    raw["live_peq"] = [{"freq": 5.0, "gain": 30.0, "q": 0.0}, {"gain": float("nan")}]
    raw["sys_peq"] = [{"gain": -99.0}] * 12
    raw.pop("live_highshelf")
    p = tmp_path / "venues.json"
    write_venues(p, [{"name": "x", "saved_at": "yesterday", "values": raw}])
    v = vn.VenueStore(p).slots[0]
    assert v is not None and v.saved_at == 0.0
    vals = v.values
    assert vals["sub_delay_ms"] == RANGES["sub_delay_ms"].max
    assert vals["top_delay_ms"] == RANGES["top_delay_ms"].min
    assert vals["balance_db"] == State().balance_db
    assert vals["lowcut_hz"] == RANGES["lowcut_hz"].max
    assert vals["sub_inverted"] is False and vals["mono_subs"] is False   # only real bools
    assert vals["live_peq"][0] == {"freq": 20.0, "gain": 6.0, "q": RANGES["live_peq.q"].min}
    assert vals["live_peq"][1]["gain"] == 0.0 and len(vals["live_peq"]) == 4
    assert all(b["gain"] == RANGES["sys_peq.gain"].min for b in vals["sys_peq"])
    assert len(vals["sys_peq"]) == 8
    assert vals["live_highshelf"] == vn.values_of(State())["live_highshelf"]
    # the same rules as loading state.json
    assert vals == vn.values_of(State.from_dict(raw))


@pytest.mark.parametrize("raw,want", [
    ("Salle des fêtes", "SALLE DES FETES"), ("  église   st-jean ", "EGLISE ST-JEAN"),
    ("Straße", "STRASSE"), ("a_b*c", "A B C"), ("x" * 20, "X" * 16), ("Hall #2 (big)", "HALL #2 (BIG)"),
    ("!!!", None), ("", None), ("   ", None), (12, None), (None, None), ("y" * 65, None),
    ("ab\u0000\ncd", "AB CD"), ("Ça", "CA"),
])
def test_names_are_sanitised_for_the_box(raw: Any, want: Any) -> None:
    out = vn.sanitize_name(raw)
    assert out == want
    if out:
        assert len(out) <= vn.NAME_MAX and set(out) <= vn.NAME_CHARS


def test_describe_changes_says_what_moves() -> None:
    now = vn.values_of(State(sub_delay_ms=0.0, lowcut_hz=112.0, balance_db=0.0))
    s = State(sub_delay_ms=2.9, lowcut_hz=90.0, balance_db=2.0, top_inverted=True, mono_subs=True)
    s.live_peq[0].gain, s.live_peq[1].gain, s.live_highshelf.gain = -3.0, -2.0, 1.0
    s.live_peq[3].freq = 3000.0            # moved while flat: inaudible, not counted
    s.sys_peq[4].gain = -1.5
    lines = vn.describe_changes(now, vn.values_of(s), rebuild_ok=True)
    assert lines == ["Sub delay 0.00 → 2.90 ms", "Top polarity normal → INVERTED", "Lo-Cut 112 → 90 Hz",
                     "Balance 0.0 → +2.0 dB (subs +1)", "Mono subs off → ON",
                     "Live EQ: 3 bands (1 louder)", "System EQ: 1 band"]
    unmuted = vn.describe_changes(now, vn.values_of(s), rebuild_ok=False)
    assert unmuted[4] == "Mono subs off → ON · MUTE FIRST"
    assert vn.describe_changes(now, now) == []
    off = vn.values_of(State(lowcut_on=False))
    assert vn.describe_changes(now, off) == ["Lo-Cut ON → OFF"]
    assert vn.describe_changes(off, now) == ["Lo-Cut OFF → ON 112 Hz"]


def test_what_can_get_louder_is_said() -> None:
    """V-1: a recall never moves master, but balance, EQ boosts, a notch taken away and Lo-Cut OFF
    can make a group or a band louder: the lines say so (and carry a LOUDER_MARKS mark)."""
    def band(f: float, g: float, q: float = 4.0) -> dict[str, float]:
        return {"freq": f, "gain": g, "q": q}

    louder = vn._louder
    assert louder(band(1000, 0), band(1000, 3))              # a boost added
    assert louder(band(1000, -9), band(1000, 0))             # a notch taken away
    assert louder(band(1000, -9), band(1000, -3))            # a cut made shallower
    assert louder(band(1000, -6), band(2000, -6))            # a cut moved: louder where it was
    assert louder(band(1000, 4), band(1000, 6))
    assert not louder(band(1000, 4), band(1000, 2))          # a boost made smaller
    assert not louder(band(1000, -3), band(1000, -6))        # a deeper cut
    assert not louder(band(1000, 0), band(1000, -6))         # a new cut
    assert not louder(band(1000, 2), band(1000, 0))          # a boost taken away
    now = vn.values_of(State(balance_db=2.0))
    for bal, want in ((8.0, "(subs +3)"), (-1.0, "(tops +1.5)"), (2.5, "(subs +0.25)")):
        line = vn.describe_changes(now, vn.values_of(State(balance_db=bal)))[0]
        assert line.endswith(want) and any(m in line for m in vn.LOUDER_MARKS), line
    notch = State()
    notch.live_peq[2].gain = -9.0
    lines = vn.describe_changes(vn.values_of(notch), vn.values_of(State()))
    assert lines == ["Live EQ: 1 band (1 louder)"]
    assert vn.describe_changes(vn.values_of(State()), vn.values_of(notch)) == ["Live EQ: 1 band"]
    off = vn.describe_changes(vn.values_of(State()), vn.values_of(State(lowcut_on=False)))
    assert off == ["Lo-Cut ON → OFF"] and any(m in off[0] for m in vn.LOUDER_MARKS)
    # nothing louder: no mark on any line
    quiet = State(sub_delay_ms=1.0, top_inverted=True, lowcut_hz=120.0)
    quiet.live_peq[0].gain = -3.0
    for line in vn.describe_changes(vn.values_of(State()), vn.values_of(quiet)):
        assert not any(m in line for m in vn.LOUDER_MARKS), line
    # the box, the phone and the store use the same marks
    assert pages.LOUDER_MARKS == vn.LOUDER_MARKS
    js = (Path(__file__).parent.parent / "k1000" / "web" / "static" / "app.js").read_text()
    assert all(json.dumps(m, ensure_ascii=False) in js for m in vn.LOUDER_MARKS)
    # and nobody tells the operator a recall cannot change the level any more
    root = Path(__file__).parent.parent.parent
    html = (Path(__file__).parent.parent / "k1000" / "web" / "static" / "index.html").read_text()
    assert "never changes the level" not in html
    assert "never how loud" not in (root / "docs" / "12-venue-snapshots.md").read_text()


def test_compact_lines_fit_the_box_and_put_mute_first_first() -> None:
    """F3: the box's half screen shows every change: the compact list is at most 7 lines (the
    polarities and the two EQs merged), the MUTE FIRST line first."""
    now = vn.values_of(State())
    wide = State(sub_delay_ms=2.9, top_delay_ms=0.75, sub_inverted=True, top_inverted=True,
                 lowcut_on=False, balance_db=4.0, mono_subs=True)
    wide.live_peq[1].gain, wide.live_lowshelf.gain, wide.sys_peq[6].gain = -4.5, 3.0, 2.0
    full = vn.describe_changes(now, vn.values_of(wide), rebuild_ok=False)
    box = vn.describe_changes(now, vn.values_of(wide), rebuild_ok=False, compact=True)
    assert len(full) == 9 and len(box) == pages.VENUE_CHANGE_ROWS == 7
    assert box[0] == "Mono subs off → ON · MUTE FIRST"
    assert "Polarity → sub INV · top INV" in box and "EQ: live 2 · system 1 (2 louder)" in box
    assert vn.describe_changes(now, vn.values_of(wide), compact=True)[0].startswith("Sub delay")
    one = State(sub_inverted=True)
    one.sys_peq[0].gain = -2.0
    assert vn.describe_changes(now, vn.values_of(one), compact=True) == [
        "Sub polarity normal → INVERTED", "System EQ: 1 band"]


def test_audible_key_ignores_what_cannot_be_heard() -> None:
    """F2: a 0 dB band anywhere is unity, the Lo-Cut frequency counts only while it is on."""
    a = State()
    b = State()
    b.live_peq[1].freq, b.live_peq[1].q = 3170.0, 12.0          # the notch helper on a flat band
    assert vn.audible_key(vn.values_of(a)) == vn.audible_key(vn.values_of(b))
    b.live_peq[1].gain = -1.0
    assert vn.audible_key(vn.values_of(a)) != vn.audible_key(vn.values_of(b))
    off1, off2 = State(lowcut_on=False, lowcut_hz=90.0), State(lowcut_on=False, lowcut_hz=120.0)
    assert vn.audible_key(vn.values_of(off1)) == vn.audible_key(vn.values_of(off2))
    assert vn.audible_key(vn.values_of(State(lowcut_hz=90.0))) != vn.audible_key(
        vn.values_of(State(lowcut_hz=120.0)))
    for k, v in (("mono_subs", True), ("sub_delay_ms", 0.5), ("balance_db", 0.5)):
        assert vn.audible_key(vn.values_of(State(**{k: v}))) != vn.audible_key(vn.values_of(State()))


def test_apply_values_touches_nothing_else() -> None:
    base = State(master_db=-7.5, muted=True, input_trim_db=3.0, out_trim_db=[1.0, -1.0, 2.0, 0.5],
                 sub_lowpass_on=True, spectrum_on=False, profile="m4")
    target = vn.values_of(venue_state())
    out = vn.apply_values(base, target)
    assert vn.values_of(out) == target
    assert never_part(out) == never_part(base)
    t2 = dict(target, mono_subs=True)
    assert vn.apply_values(base, t2, rebuild=False).mono_subs is False
    assert vn.apply_values(base, t2).mono_subs is True
    assert vn.values_of(base) == vn.values_of(State(master_db=-7.5, muted=True, input_trim_db=3.0,
                                                   out_trim_db=[1.0, -1.0, 2.0, 0.5],
                                                   sub_lowpass_on=True, spectrum_on=False,
                                                   profile="m4"))   # the base is not edited


# ======================================================================= the Controller
def ctl_with_slot(tmp_path: Path, slot_state: State, start: State | None = None, *,
                  slot: int = 0, name: str = "Hall", **kw: Any) -> tuple[Controller, Any]:
    write_venues(tmp_path / "venues.json", [None] * slot + [slot_dict(slot_state, name)])
    return make(tmp_path, start, playing=True, **kw)


def test_paths_venues_default_and_setting(tmp_path: Path) -> None:
    p = Paths(tmp_path / "s.json", tmp_path / "a.yml")
    assert p.venues_file == tmp_path / "venues.json"
    # V-6: the shipped k1000.yaml leaves paths.venues unset, so a run with --state elsewhere keeps
    # its own slots and never writes the live venues.json
    from k1000 import profiles as pf
    shipped = pf.load_settings(Path(__file__).parent.parent / "etc" / "k1000.yaml")
    assert "venues" not in (shipped.get("paths") or {})
    dev = Paths.from_settings(shipped, state=tmp_path / "x.json")
    assert dev.venues_file == tmp_path / "venues.json"
    assert Paths.from_settings(shipped).venues_file == Path("/var/lib/k1000/venues.json")
    q = Paths.from_settings({"paths": {"state": "/x/s.json", "venues": "/y/v.json"}})
    assert q.venues_file == Path("/y/v.json") and q.state == Path("/x/s.json")
    assert Paths.from_settings({}).venues_file == Path("/var/lib/k1000/venues.json")
    assert Paths.from_settings({}, venues="/z.json").venues_file == Path("/z.json")


def test_recall_changes_only_the_venue_values(tmp_path: Path) -> None:
    """Only the listed keys change; master and mute are never written to the engine; trims,
    input trim, profile stay; the recalled values reach the engine; state.json holds them."""
    async def body() -> None:
        start = State(master_db=-14.0, input_trim_db=2.0, out_trim_db=[0.5, 0.5, -1.0, -1.0],
                      sub_lowpass_on=True)
        target = venue_state()
        ctl, cl = ctl_with_slot(tmp_path, target, start)
        await boot(ctl)
        before_never = never_part(ctl.state)
        mark = len(cl.log)
        ok, msg = ctl.recall_venue(0)
        assert ok and msg.startswith("RECALLED HALL")
        assert venue_part(ctl.state) == vn.values_of(target)
        assert never_part(ctl.state) == before_never
        assert await ctl.settle(timeout=5.0)
        log = cl.log[mark:]
        assert not [e for e in log if e[0] in ("fader", "mute") and e[1] == 0]    # Main untouched
        assert not [e for e in log if e[0] == "config"]                          # no rebuild
        assert dc.param_map(ctl._applied) == dc.param_map(ctl.state)
        for ptr, want in dc.param_map(ctl.state).items():
            node: Any = cl.cfg
            for part in ptr.strip("/").split("/"):
                node = node[part]
            assert node == want, ptr
        assert cl.faders[0] == {"volume": -14.0, "mute": False}
        await until(lambda: (tmp_path / "state.json").exists() and json.loads(
            (tmp_path / "state.json").read_text())["sub_delay_ms"] == 2.9, what="saved")
        saved = State.from_dict(json.loads((tmp_path / "state.json").read_text()))
        assert venue_part(saved) == vn.values_of(target) and saved.master_db == -14.0
        await ctl.stop()

    run(body())


def test_recall_delay_and_polarity_under_the_guard_dip_eq_in_steps(tmp_path: Path) -> None:
    async def body() -> None:
        target = State(sub_delay_ms=3.0, top_inverted=True)
        target.live_peq[0].gain = -6.0
        ctl, cl = ctl_with_slot(tmp_path, target)
        await boot(ctl)
        mark = len(cl.log)
        assert ctl.recall_venue(0)[0]
        assert await ctl.settle(timeout=5.0)
        log = nomark(cl.log[mark:])
        delay = ("set", "/filters/sub_delay/parameters/delay", dc.delay_on_grid(3.0))
        # the delay write (alone or in a PatchConfig) sits between the Aux1 mute and unmute
        i_mute, i_unmute = log.index(("mute", 1, True)), log.index(("mute", 1, False))
        writes = [i for i, e in enumerate(log) if e == delay or (
            e[0] == "patch" and "sub_delay" in json.dumps(e[1]))]
        assert writes and all(i_mute < i < i_unmute for i in writes)
        assert ("mute", 2, True) in log and ("mute", 2, False) in log         # polarity: tops dip
        gains = sets_for(log, "/filters/live_peq_1/parameters/gain")
        steps = [0.0] + gains
        assert gains[-1] == -6.0 and all(abs(b - a) <= 0.5 + 1e-9 for a, b in zip(steps, steps[1:]))
        assert not [e for e in log if e[0] == "config"]
        assert cl.faders[1]["mute"] is False and cl.faders[2]["mute"] is False
        await ctl.stop()

    run(body())


def test_undo_restores_the_exact_values(tmp_path: Path) -> None:
    async def body() -> None:
        start = State(master_db=-12.0, sub_delay_ms=1.25, balance_db=-1.5)
        start.live_peq[1].freq, start.live_peq[1].gain, start.live_peq[1].q = 3170.0, -4.5, 6.3
        start.sys_peq[0].gain = 1.5
        ctl, cl = ctl_with_slot(tmp_path, venue_state(), start)
        await boot(ctl)
        before = venue_part(ctl.state)
        assert ctl.recall_venue(0)[0]
        assert await ctl.settle(timeout=5.0)
        assert ctl.venue_undo_left() > UNDO_RECALL_S - 5
        ctl.nudge("master_db", -2)                 # master is not a venue value: undo stays
        assert ctl.venue_undo_left() > 0 and ctl.venue_now() == 0
        ok, msg = ctl.undo_recall()
        assert ok and msg == "RECALL UNDONE"
        assert venue_part(ctl.state) == before
        assert ctl.state.master_db == -13.0
        assert await ctl.settle(timeout=5.0)
        assert dc.param_map(ctl._applied) == dc.param_map(ctl.state)
        assert ctl.venue_undo_left() == 0.0 and not ctl.undo_recall()[0]        # one level
        assert ctl.venue_now() is None
        await ctl.stop()

    run(body())


def test_undo_and_pill_end_at_the_first_edit_or_after_5_min(tmp_path: Path) -> None:
    ctl, _ = ctl_with_slot(tmp_path, venue_state())
    t = [1000.0]
    ctl._ui_clock = lambda: t[0]
    assert ctl.recall_venue(0)[0]
    assert ctl.venue_now() == 0 and "VENUE: HALL" in ctl.pills()
    t[0] += UNDO_RECALL_S - 1
    assert ctl.venue_undo_left() == pytest.approx(1.0)
    t[0] += 2
    assert ctl.venue_undo_left() == 0.0 and not ctl.undo_recall()[0]
    assert ctl.venue_now() == 0                       # the pill stays: values still the slot's
    ctl.nudge("live_peq.0.gain", -1)                  # the first edit
    assert ctl.venue_now() is None and not any(p.startswith("VENUE") for p in ctl.pills())
    ctl.nudge("live_peq.0.gain", 1)                   # back to the same values: gone for good
    assert ctl.venue_now() is None
    # an edit inside the 5 min drops the undo, and says so
    ctl.nudge("live_peq.0.gain", -2)
    t[0] = 5000.0
    assert ctl.recall_venue(0)[1] == "RECALLED HALL · UNDO IN VENUES"
    ctl.set_value("sub_delay_ms", 4.0)
    assert ctl.venue_undo_left() == 0.0 and ctl.focus.hint == "RECALL UNDO LOST"
    assert ctl.undo_recall() == (False, "NO RECALL TO UNDO")


def test_recall_while_muted_applies_mono_subs_and_stays_muted(tmp_path: Path) -> None:
    async def body() -> None:
        target = venue_state()
        target.mono_subs = True
        ctl, cl = ctl_with_slot(tmp_path, target, State(muted=True))
        await boot(ctl)
        assert ctl.displayed_muted()
        mark = len(cl.log)
        ok, msg = ctl.recall_venue(0)
        assert ok and "MONO" not in msg and ctl.state.mono_subs is True
        assert await ctl.settle(timeout=5.0)
        log = cl.log[mark:]
        assert [e for e in log if e[0] == "config"]          # the mixer change: one upload, muted
        assert ("mute", 0, False) not in log and cl.faders[0]["mute"] is True
        assert ctl._applied.mono_subs is True and ctl.state.muted is True
        await ctl.stop()

    run(body())


def test_recall_unmuted_keeps_mono_subs_and_says_so(tmp_path: Path) -> None:
    async def body() -> None:
        target = venue_state()
        target.mono_subs = True
        ctl, cl = ctl_with_slot(tmp_path, target)
        await boot(ctl)
        assert "Mono subs off → ON · MUTE FIRST" in (ctl.venue_changes(0) or [])
        mark = len(cl.log)
        ok, msg = ctl.recall_venue(0)
        assert ok and msg == "RECALLED HALL · MONO SUB NOT SET (MUTE FIRST)"
        assert ctl.state.mono_subs is False
        assert await ctl.settle(timeout=5.0)
        assert not [e for e in cl.log[mark:] if e[0] == "config"]
        # F1/V-3: the sound is not the slot's (mono subs differs): no pill, no NOW mark, and the
        # open slot still says what a recall would change
        assert ctl.venue_now() is None and not any(p.startswith("VENUE") for p in ctl.pills())
        assert ctl.venue_changes(0) == ["Mono subs off → ON · MUTE FIRST"]
        # recalled again unmuted: nothing else to change, and it says why it is not the slot yet
        assert ctl.recall_venue(0) == (True, "HALL: MONO SUB NOT SET (MUTE FIRST)")
        assert ctl.venue_now() is None
        await ctl.stop()

    run(body())


def test_recall_refusals(tmp_path: Path) -> None:
    ctl, _ = ctl_with_slot(tmp_path, venue_state(), slot=3)
    before = ctl.state.to_dict()
    assert ctl.recall_venue(0) == (False, "SLOT 1 IS EMPTY")
    assert ctl.recall_venue(8) == (False, "NO SUCH SLOT")
    assert ctl.recall_venue(True)[0] is False           # type: ignore[arg-type]
    assert ctl.recall_venue(3, expect_saved_at=123.0) == (False, "SLOT 4 CHANGED MEANWHILE: LOOK AGAIN")
    assert ctl.state.to_dict() == before
    assert ctl.recall_venue(3, expect_saved_at=1760000000.0)[0]
    assert ctl.recall_venue(3) == (True, "HALL: NOTHING TO CHANGE")
    assert ctl.rename_venue(0, "x") == (False, "SLOT 1 IS EMPTY")
    assert ctl.rename_venue(3, "!!") == (False, "NAME: LETTERS OR DIGITS NEEDED")
    assert ctl.save_venue(9) == (False, "NO SUCH SLOT")
    assert ctl.save_venue(1, "%%%") == (False, "NAME: LETTERS OR DIGITS NEEDED")
    assert ctl.venues.get(1) is None


def test_recall_hint_wins_over_the_reset_undo_lost(tmp_path: Path) -> None:
    """V-2: a recall that ends the live-EQ reset's undo still shows ITS hint (the mono warning
    must not be replaced by RESET UNDO LOST)."""
    target = venue_state()
    target.mono_subs = True
    start = State()
    start.live_peq[1].gain = -3.0
    ctl, _ = ctl_with_slot(tmp_path, target, start)
    ctl.reset_live()
    assert ctl.undo_left() > 0
    ok, msg = ctl.recall_venue(0)
    assert ok and msg == "RECALLED HALL · MONO SUB NOT SET (MUTE FIRST)"
    assert ctl.focus.hint == msg and ctl.undo_left() == 0.0
    # undo_recall too: the reset's undo is gone already, its hint is the recall undo's
    assert ctl.undo_recall() == (True, "RECALL UNDONE") and ctl.focus.hint == "RECALL UNDONE"


def test_pill_follows_what_the_slot_holds_now(tmp_path: Path) -> None:
    """V-3: on slot 1 (A), recall slot 2 (B), save B into slot 1, undo: back on A's values, but
    slot 1 holds B now: no pill, no NOW. A slot saved over from the phone drops the pill too."""
    a, b = venue_state(), State(sub_delay_ms=1.5, balance_db=-2.0)
    write_venues(tmp_path / "venues.json", [slot_dict(a, "Room A"), slot_dict(b, "Room B")])
    ctl, _ = make(tmp_path, a)
    assert ctl.venue_now() == 0
    assert ctl.recall_venue(1)[0] and ctl.venue_now() == 1
    assert ctl.save_venue(0)[0] and ctl.venue_now() == 0     # slot 1 (named ROOM A) holds B now
    assert ctl.undo_recall()[0]
    assert venue_part(ctl.state) == vn.values_of(a)
    assert ctl.venue_now() is None and not any(p.startswith("VENUE") for p in ctl.pills())
    # the pill is on slot 2; the phone saves other values into slot 2: gone
    ctl.recall_venue(1)
    assert ctl.venue_now() == 1
    ctl.venues.put(1, vn.Venue("ROOM B", 9.0, vn.values_of(State(sub_delay_ms=4.0))))
    assert ctl.venue_now() is None


def test_inaudible_moves_keep_the_pill_and_the_undo(tmp_path: Path) -> None:
    """F2: the notch helper's B4 (Q/freq on a free band, gain untouched) or a FREQ turn on a flat
    band changes nothing audible: the pill and the recall undo stay, as the reset undo does. The
    undo still restores the exact values from before the recall."""
    target = venue_state()
    start = State(sub_delay_ms=0.8)
    start.live_peq[3].freq, start.live_peq[3].q = 4000.0, 2.0
    ctl, _ = ctl_with_slot(tmp_path, target, start)
    before = venue_part(ctl.state)
    assert ctl.recall_venue(0)[0]
    ctl.focus.slot = 1
    assert ctl.notch_helper() == 1                         # P2 is flat in the slot
    assert ctl.state.live_peq[1].q != target.live_peq[1].q
    assert ctl.venue_now() == 0 and ctl.venue_undo_left() > 0
    assert "VENUE: HALL" in ctl.pills() and ctl.venue_changes(0) == []
    ctl.nudge("live_peq.3.freq", 2)                        # a FREQ turn on a flat band
    assert ctl.venue_now() == 0 and ctl.venue_undo_left() > 0
    ok, _msg = ctl.undo_recall()
    assert ok and venue_part(ctl.state) == before          # exact, over the inaudible moves
    # an audible edit still ends both, and says so
    assert ctl.recall_venue(0)[0]
    ctl.nudge("live_peq.1.gain", -1)
    assert ctl.venue_now() is None and ctl.venue_undo_left() == 0.0
    assert ctl.focus.hint == "RECALL UNDO LOST"


def test_undo_that_needs_a_rebuild_waits_for_mute(tmp_path: Path) -> None:
    """V-4: a muted recall changed mono subs; unmuted, the undo would need a rebuild: refused
    and KEPT (MUTE FIRST TO UNDO); muted again, it restores everything."""
    target = venue_state()
    target.mono_subs = True
    ctl, _ = ctl_with_slot(tmp_path, target, State(muted=True))
    before = venue_part(ctl.state)
    assert ctl.displayed_muted()
    assert ctl.recall_venue(0)[0] and ctl.state.mono_subs is True
    ctl.state.muted = False                               # the operator unmutes
    assert not ctl.displayed_muted()
    assert ctl.undo_recall() == (False, "MUTE FIRST TO UNDO (MONO SUB)")
    assert ctl.venue_undo_left() > 0 and ctl.state.mono_subs is True
    assert ctl.focus.hint == "MUTE FIRST TO UNDO (MONO SUB)"
    ctl.state.muted = True
    assert ctl.undo_recall() == (True, "RECALL UNDONE")
    assert venue_part(ctl.state) == before and ctl.state.mono_subs is False
    # an unmuted recall that kept mono subs undoes unmuted (nothing to rebuild)
    ctl.state.muted = False
    assert ctl.recall_venue(0)[0] and ctl.state.mono_subs is False
    assert ctl.undo_recall() == (True, "RECALL UNDONE")


def test_box_recall_refused_when_the_slot_was_saved_since_the_confirm(tmp_path: Path) -> None:
    """V-5 on the Controller: the box's RECALL arms on slot 1 as shown; the phone saves into it;
    the second push only re-arms; RecallVenue with a stale saved time is refused."""
    target = venue_state()
    write_venues(tmp_path / "venues.json", [slot_dict(target, "Hall", 1760000000.0)])
    ctl, _ = make(tmp_path)
    ctl._wall_clock = lambda: 1767225600.0
    before = venue_part(ctl.state)
    ctl.goto_page("VENUES")
    ctl.focus.venue, ctl.focus.venue_mode, ctl.focus.venue_menu = 0, "menu", 0
    ctl.handle_event(Sw(0, "short", ts=1.0))
    assert ctl.focus.hint == "PUSH AGAIN TO RECALL HALL"
    assert ctl.save_venue(0, "Crypt", source="phone")[0]     # the phone, between the pushes
    ctl.handle_event(Sw(0, "short", ts=2.0))
    assert ctl.focus.hint == "PUSH AGAIN TO RECALL CRYPT" and venue_part(ctl.state) == before
    ctl.handle_action(RecallVenue(0, 1760000000.0))         # a stale action: refused
    assert ctl.focus.hint == "SLOT 1 CHANGED MEANWHILE: LOOK AGAIN"
    assert venue_part(ctl.state) == before
    ctl.focus.venue_mode = "menu"
    ctl.handle_event(Sw(0, "short", ts=3.0))                 # confirmed on what is there now
    assert ctl.focus.hint == "CRYPT: NOTHING TO CHANGE"


def test_out_of_range_slot_values_are_clamped_on_recall(tmp_path: Path) -> None:
    raw = vn.values_of(State())
    raw.update(sub_delay_ms=500.0, balance_db=40.0)
    raw["live_peq"] = [{"freq": 1000.0, "gain": 25.0, "q": 99.0}] * 4
    write_venues(tmp_path / "venues.json", [{"name": "Wild", "saved_at": 1.0, "values": raw}])
    ctl, _ = make(tmp_path)
    assert ctl.recall_venue(0)[0]
    s = ctl.state
    assert s.sub_delay_ms == 50.0 and s.balance_db == 12.0
    assert all(b.gain == 6.0 and b.q == 16.0 for b in s.live_peq)


def test_save_rename_and_the_pill(tmp_path: Path) -> None:
    async def body() -> None:
        ctl, cl = make(tmp_path, venue_state(), playing=True)
        ctl._wall_clock = lambda: 1767225600.25
        await boot(ctl)
        mark = len(cl.log)
        ok, msg = ctl.save_venue(2)
        assert ok and msg == "SAVED IN SLOT 3 · VENUE 3"
        assert ctl.venue_now() == 2 and ctl.pills()[-1] == "VENUE: VENUE 3"
        assert ctl.focus.venue_names[2] == "VENUE 3"
        assert await ctl.flush_venues(2.0)
        data = json.loads((tmp_path / "venues.json").read_text())
        assert data["slots"][2] == {"name": "VENUE 3", "saved_at": 1767225600.25,
                                    "values": vn.values_of(ctl.state)}
        assert ctl.rename_venue(2, "Salle des fêtes")[1] == "SLOT 3 IS NOW SALLE DES FETES"
        ctl.nudge("balance_db", 1)
        assert ctl.save_venue(2)[0]                       # an overwrite keeps the name
        await ctl.flush_venues(2.0)
        again = vn.VenueStore(tmp_path / "venues.json").slots[2]
        assert again is not None and again.name == "SALLE DES FETES"
        assert again.values["balance_db"] == 2.5
        assert ctl.save_venue(5, "Garden")[0]
        await ctl.flush_venues(2.0)
        assert vn.VenueStore(tmp_path / "venues.json").slots[5].name == "GARDEN"   # type: ignore[union-attr]
        assert ctl.venue_now() == 5
        assert await ctl.settle(timeout=3.0)
        # saving and renaming send nothing to the engine: only the balance nudge's Aux faders
        assert cl.log[mark:] and all(e[0] == "fader" and e[1] in (1, 2) for e in cl.log[mark:])
        await ctl.stop()

    run(body())


def test_a_failed_venues_write_says_so_and_keeps_the_slot(tmp_path: Path) -> None:
    blocker = tmp_path / "ro"
    blocker.write_text("a file, not a directory")
    ctl, _ = make(tmp_path)
    ctl.venues.path = blocker / "venues.json"
    assert ctl.save_venue(0, "x")[0]
    assert ctl.venues.get(0) is not None
    assert ("warning", "VENUES NOT SAVED (DISK)") in ctl.alerts()
    ctl.venues.path = tmp_path / "venues.json"
    ctl.save_venue(1, "y")
    assert ("warning", "VENUES NOT SAVED (DISK)") not in ctl.alerts()
    assert [v.name for v in vn.VenueStore(tmp_path / "venues.json").slots if v] == ["X", "Y"]


def test_torn_venues_file_never_stops_the_controller(tmp_path: Path, caplog: Any) -> None:
    (tmp_path / "venues.json").write_text('{"schema": 1, "slots": [{"name": "a", "val')
    with caplog.at_level(logging.WARNING):
        ctl, _ = make(tmp_path)
    assert ctl.venues.slots == [None] * 8 and ctl.status.venues_error
    assert (tmp_path / "venues.json.bad").exists()
    page, st, status, focus = ctl.snapshot()
    assert status.venues == [None] * 8 and status.venues_error
    pages.render("VENUES", st, status, focus)            # the page says so, no crash


def test_booting_on_a_venues_values_shows_its_pill(tmp_path: Path) -> None:
    s = venue_state()
    write_venues(tmp_path / "venues.json", [None, slot_dict(s, "Chapel")])
    ctl, _ = make(tmp_path, s)
    assert ctl.venue_now() == 1 and ctl.pills()[-1] == "VENUE: CHAPEL"
    _page, _st, status, _f = ctl.snapshot()
    assert status.venue_slot == 1 and status.venue_name == "CHAPEL"
    assert status.venues[1][0] == "CHAPEL"


def test_snapshot_lists_what_a_recall_would_change_on_the_open_slot(tmp_path: Path) -> None:
    ctl, _ = ctl_with_slot(tmp_path, venue_state())
    ctl.goto_page("VENUES")
    assert ctl.snapshot()[2].venue_changes == []         # the list: nothing open
    ctl.focus.venue_mode, ctl.focus.venue = "menu", 0
    ch = ctl.snapshot()[2].venue_changes
    assert ch and ch[0] == "Sub delay 0.00 → 2.90 ms"
    ctl.goto_page("SHOW")
    assert ctl.focus.venue_mode == "list" and ctl.snapshot()[2].venue_changes == []


def test_box_knobs_drive_a_whole_recall(tmp_path: Path) -> None:
    """SETUP > Venues (push) -> VENUES; E1 to slot 2; push opens it on RECALL; push = hint; push
    again = recall; the Undo row (E2 down) undoes it with one push."""
    target = venue_state()
    write_venues(tmp_path / "venues.json", [None, slot_dict(target, "Chapel")])
    ctl, _ = make(tmp_path)
    before = venue_part(ctl.state)
    ctl.goto_page("SETUP")
    ctl.focus.item = [i.field for i in im.SETUP_ITEMS].index("VENUES")
    ctl.handle_event(Sw(0, "short", ts=1.0))
    assert ctl.page == "VENUES" and ctl.focus.venue_mode == "list"
    ctl.handle_event(Enc(0, 1, ts=2.0))
    ctl.handle_event(Sw(1, "short", ts=3.0))
    assert ctl.focus.venue_mode == "menu" and ctl.focus.venue_menu == 0
    ctl.handle_event(Sw(0, "short", ts=4.0))
    assert ctl.focus.hint == "PUSH AGAIN TO RECALL CHAPEL" and venue_part(ctl.state) == before
    ctl.handle_event(Sw(0, "short", ts=5.0))
    assert venue_part(ctl.state) == vn.values_of(target)
    assert ctl.focus.venue_mode == "list"
    ctl.handle_event(Enc(1, 20, ts=6.0))
    assert ctl.focus.venue == im.VENUE_UNDO_ROW and ctl.focus.venue_undo_ok
    ctl.handle_event(Sw(0, "short", ts=7.0))           # F4: one push only arms
    assert ctl.focus.hint == im.HINT_VENUE_UNDO and venue_part(ctl.state) == vn.values_of(target)
    ctl.handle_event(Sw(0, "short", ts=8.0))
    assert venue_part(ctl.state) == before
    assert not ctl.focus.venue_undo_ok
    ctl.handle_event(Sw(0, "short", ts=9.0))           # nothing left to undo: said at once
    assert ctl.focus.hint == im.HINT_VENUE_NO_UNDO and ctl.focus.confirm is None


# ======================================================================= the VENUES input map
NAMES = ("HALL", None, "CHAPEL", None, None, None, None, None)


def vf(**kw: Any) -> Focus:
    return Focus(venue_names=NAMES, **kw)


def test_list_scrolls_with_e1_or_e2_and_clamps() -> None:
    f = vf()
    assert map_event("VENUES", f, Enc(0, 1)) == [] and f.venue == 1
    assert map_event("VENUES", f, Enc(1, 3)) == [] and f.venue == 4
    map_event("VENUES", f, Enc(0, 50))
    assert f.venue == im.VENUE_UNDO_ROW == 8
    map_event("VENUES", f, Enc(1, -50))
    assert f.venue == 0
    assert map_event("VENUES", f, Enc(2, 1)) == [] and map_event("VENUES", f, Enc(3, 1)) == []
    assert im.knob_labels("VENUES") == ["SLOT", "SLOT", "", ""]


def test_push_opens_a_slot_on_recall_or_on_save_here_when_empty() -> None:
    f = vf()
    assert map_event("VENUES", f, Sw(0, "short")) == []
    assert f.venue_mode == "menu" and im.VENUE_MENU[f.venue_menu] == "RECALL"
    map_event("VENUES", f, Enc(0, 3))                     # BACK
    assert im.VENUE_MENU[f.venue_menu] == "BACK"
    map_event("VENUES", f, Sw(1, "short"))
    assert f.venue_mode == "list"
    f.venue = 1
    map_event("VENUES", f, Sw(0, "short"))
    assert im.VENUE_MENU[f.venue_menu] == "SAVE HERE"


def test_recall_needs_a_second_push_on_the_same_slot_within_5_s() -> None:
    f = vf(venue=2, venue_mode="menu", venue_menu=0)
    assert map_event("VENUES", f, Sw(0, "short", ts=10.0)) == [ShowHint("PUSH AGAIN TO RECALL CHAPEL")]
    assert f.confirm == "venue_recall:2"
    assert map_event("VENUES", f, Sw(1, "short", ts=11.0)) == [RecallVenue(2)]
    assert f.confirm is None and f.venue_mode == "list"
    # expired
    f = vf(venue=2, venue_mode="menu", venue_menu=0)
    map_event("VENUES", f, Sw(0, "short", ts=10.0))
    assert map_event("VENUES", f, Sw(0, "short", ts=15.5)) == [ShowHint("PUSH AGAIN TO RECALL CHAPEL")]
    # a turn cancels
    map_event("VENUES", f, Enc(0, 1, ts=16.0))
    map_event("VENUES", f, Enc(0, -1, ts=16.1))
    assert f.confirm is None
    assert map_event("VENUES", f, Sw(0, "short", ts=16.2)) == [ShowHint("PUSH AGAIN TO RECALL CHAPEL")]
    # an empty slot cannot be recalled
    g = vf(venue=1, venue_mode="menu", venue_menu=0)
    assert map_event("VENUES", g, Sw(0, "short", ts=1.0)) == [ShowHint(im.HINT_VENUE_EMPTY)]
    assert map_event("VENUES", g, Sw(0, "short", ts=1.5)) == [ShowHint(im.HINT_VENUE_EMPTY)]


def test_save_here_confirms_and_an_overwrite_says_so() -> None:
    f = vf(venue=1, venue_mode="menu", venue_menu=1)
    assert map_event("VENUES", f, Sw(0, "short", ts=1.0)) == [ShowHint("PUSH AGAIN TO SAVE IN SLOT 2")]
    assert map_event("VENUES", f, Sw(0, "short", ts=2.0)) == [SaveVenue(1)]
    g = vf(venue=0, venue_mode="menu", venue_menu=1)
    assert map_event("VENUES", g, Sw(0, "short", ts=1.0)) == [ShowHint("PUSH AGAIN TO OVERWRITE HALL")]
    assert map_event("VENUES", g, Sw(1, "short", ts=1.2)) == [SaveVenue(0)]
    # the confirm of one action never confirms another
    h = vf(venue=0, venue_mode="menu", venue_menu=1)
    map_event("VENUES", h, Sw(0, "short", ts=1.0))
    h.venue_menu = 0
    assert map_event("VENUES", h, Sw(0, "short", ts=1.1)) == [ShowHint("PUSH AGAIN TO RECALL HALL")]


def test_rename_letter_picker() -> None:
    f = vf(venue=2, venue_mode="menu", venue_menu=2)
    assert map_event("VENUES", f, Sw(0, "short")) == [ShowHint(im.HINT_VENUE_RENAME, 4.0)]
    assert f.venue_mode == "rename" and "".join(f.venue_buf).strip() == "CHAPEL"
    assert f.venue_cursor == 6                            # F5: opens just after the name
    map_event("VENUES", f, Enc(0, -6))
    map_event("VENUES", f, Enc(1, 1))                     # C -> D
    assert f.venue_buf[0] == "D"
    map_event("VENUES", f, Enc(0, 6))                     # to the first blank cell
    map_event("VENUES", f, Enc(1, 2))                     # ' ' -> A -> B
    map_event("VENUES", f, Enc(1, -1))                    # B -> A
    assert "".join(f.venue_buf).rstrip() == "DHAPELA"
    map_event("VENUES", f, Enc(1, -1))                    # A -> ' '
    map_event("VENUES", f, Enc(1, -1))                    # ' ' wraps to the last mark
    assert f.venue_buf[6] == vn.NAME_PICKER[-1]
    map_event("VENUES", f, Enc(1, 1))
    assert f.venue_buf[6] == " "
    assert map_event("VENUES", f, Sw(0, "short")) == [] and f.venue_cursor == 7   # push = next
    map_event("VENUES", f, Enc(0, 100))
    assert f.venue_cursor == im.VENUE_CANCEL
    map_event("VENUES", f, Enc(0, -2))
    assert f.venue_cursor == im.VENUE_OK
    assert map_event("VENUES", f, Sw(1, "short")) == [RenameVenue(2, "DHAPEL")]
    assert f.venue_mode == "menu"
    # cancel, and an all-blank name
    g = vf(venue=0, venue_mode="rename", venue_buf=list(" " * 16), venue_cursor=im.VENUE_OK)
    assert map_event("VENUES", g, Sw(0, "short")) == [ShowHint(im.HINT_VENUE_NO_NAME)]
    g.venue_cursor = im.VENUE_CANCEL
    assert map_event("VENUES", g, Sw(0, "short")) == [ShowHint(im.HINT_VENUE_CANCELLED)]
    assert g.venue_mode == "menu"
    h = vf(venue=1, venue_mode="menu", venue_menu=2)       # nothing to rename in an empty slot
    assert map_event("VENUES", h, Sw(0, "short")) == [ShowHint(im.HINT_VENUE_EMPTY)]


def test_rename_is_quick_two_blanks_end_the_name_and_clear() -> None:
    """F5: SALLE DES FETES -> BAR in a handful of moves: CLEAR, three letters, pushes to OK (two
    blanks in a row end the name; a single blank, a space inside a name, is stepped over)."""
    names = ("SALLE DES FETES",) + (None,) * 7
    f = Focus(venue_names=names, venue=0, venue_mode="menu", venue_menu=2)
    map_event("VENUES", f, Sw(0, "short", ts=1.0))
    assert f.venue_mode == "rename" and f.venue_cursor == 15
    map_event("VENUES", f, Enc(0, 2, ts=2.0))
    assert f.venue_cursor == im.VENUE_CLEAR
    assert map_event("VENUES", f, Sw(0, "short", ts=3.0)) == [ShowHint(im.HINT_VENUE_CLEARED)]
    assert f.venue_buf == [" "] * 16 and f.venue_cursor == 0
    for k, letter in enumerate("BAR"):
        map_event("VENUES", f, Enc(1, vn.NAME_PICKER.index(letter), ts=4.0 + k))
        assert map_event("VENUES", f, Sw(0, "short", ts=4.5 + k)) == []
    assert f.venue_cursor == 3
    map_event("VENUES", f, Sw(0, "short", ts=8.0))       # one blank: could be a space, step on
    assert f.venue_cursor == 4
    map_event("VENUES", f, Sw(0, "short", ts=8.5))       # a second blank: the name is done
    assert f.venue_cursor == im.VENUE_OK
    assert map_event("VENUES", f, Sw(0, "short", ts=9.0)) == [RenameVenue(0, "BAR")]
    # a space inside a name: one push over the blank, then the next word
    g = Focus(venue_names=("GARDEN",) + (None,) * 7, venue=0, venue_mode="menu", venue_menu=2)
    map_event("VENUES", g, Sw(0, "short"))
    assert g.venue_cursor == 6
    map_event("VENUES", g, Sw(0, "short"))
    map_event("VENUES", g, Enc(1, vn.NAME_PICKER.index("B")))
    map_event("VENUES", g, Sw(0, "short"))
    map_event("VENUES", g, Sw(0, "short"))
    map_event("VENUES", g, Sw(0, "short"))
    assert map_event("VENUES", g, Sw(0, "short")) == [RenameVenue(0, "GARDEN B")]
    # a full 16-letter name opens on OK: one push keeps it
    full = Focus(venue_names=("ABCDEFGHIJKLMNOP",) + (None,) * 7, venue=0, venue_mode="menu",
                 venue_menu=2)
    map_event("VENUES", full, Sw(0, "short"))
    assert full.venue_cursor == im.VENUE_OK
    assert map_event("VENUES", full, Sw(0, "short")) == [RenameVenue(0, "ABCDEFGHIJKLMNOP")]


def test_undo_row_needs_two_pushes() -> None:
    """F4: the Undo recall row is where an overshoot lands: one push never undoes."""
    f = vf(venue=im.VENUE_UNDO_ROW, venue_undo_ok=True)
    assert map_event("VENUES", f, Sw(0, "short", ts=1.0)) == [ShowHint(im.HINT_VENUE_UNDO)]
    assert f.confirm == im.VENUE_UNDO_KEY
    assert map_event("VENUES", f, Sw(1, "short", ts=2.0)) == [UndoRecall()]
    assert f.confirm is None
    # expired, or a turn in between: arms again
    map_event("VENUES", f, Sw(0, "short", ts=10.0))
    assert map_event("VENUES", f, Sw(0, "short", ts=15.5)) == [ShowHint(im.HINT_VENUE_UNDO)]
    map_event("VENUES", f, Enc(0, -1, ts=16.0))
    map_event("VENUES", f, Enc(0, 1, ts=16.1))
    assert map_event("VENUES", f, Sw(0, "short", ts=16.2)) == [ShowHint(im.HINT_VENUE_UNDO)]
    # nothing to undo: said at once, nothing armed
    g = vf(venue=im.VENUE_UNDO_ROW)
    assert map_event("VENUES", g, Sw(0, "short", ts=1.0)) == [ShowHint(im.HINT_VENUE_NO_UNDO)]
    assert g.confirm is None
    assert map_event("VENUES", g, Sw(0, "short", ts=1.5)) == [ShowHint(im.HINT_VENUE_NO_UNDO)]


def test_box_confirm_is_bound_to_the_slot_content() -> None:
    """V-5: a slot saved again (phone) between the two pushes: the second push re-arms on what
    the slot holds now; RecallVenue carries the saved time the confirm was armed on."""
    f = vf(venue=2, venue_mode="menu", venue_menu=0, venue_stamps=(1.0, None, 1760000000.0))
    map_event("VENUES", f, Sw(0, "short", ts=1.0))
    assert f.confirm == "venue_recall:2@1760000000.000"
    f.venue_names, f.venue_stamps = ("HALL", None, "CRYPT"), (1.0, None, 1760000999.0)  # the phone
    assert map_event("VENUES", f, Sw(0, "short", ts=2.0)) == [ShowHint("PUSH AGAIN TO RECALL CRYPT")]
    assert map_event("VENUES", f, Sw(0, "short", ts=3.0)) == [RecallVenue(2, 1760000999.0)]
    # SAVE HERE too: an empty slot filled meanwhile is not silently overwritten
    g = vf(venue=1, venue_mode="menu", venue_menu=1, venue_stamps=(1.0, None, 2.0))
    map_event("VENUES", g, Sw(0, "short", ts=1.0))
    g.venue_names, g.venue_stamps = ("HALL", "NEW", "CHAPEL"), (1.0, 5.0, 2.0)
    assert map_event("VENUES", g, Sw(0, "short", ts=2.0)) == [ShowHint("PUSH AGAIN TO OVERWRITE NEW")]
    assert map_event("VENUES", g, Sw(0, "short", ts=3.0)) == [SaveVenue(1)]


def test_no_button_ever_recalls_saves_or_undoes() -> None:
    venue_acts = (RecallVenue, SaveVenue, RenameVenue, UndoRecall)
    for mode, menu in (("list", 0), ("menu", 0), ("menu", 1), ("rename", 0)):
        for sw in range(4, 8):
            for kind in ("press", "short", "long", "release"):
                f = vf(venue=0, venue_mode=mode, venue_menu=menu, confirm="venue_recall:0",
                       confirm_until=math.inf)
                for a in map_event("VENUES", f, Sw(sw, kind)):
                    assert not isinstance(a, venue_acts), (mode, sw, kind, a)
    # E3/E4 pushes and long pushes never select
    for n in (2, 3):
        assert map_event("VENUES", vf(), Sw(n, "short")) == []
    assert map_event("VENUES", vf(), Sw(0, "long")) == []


def test_a_push_after_a_fine_turn_is_not_a_select() -> None:
    f = vf(venue=2, venue_mode="menu", venue_menu=0, confirm="venue_recall:2", confirm_until=math.inf)
    map_event("VENUES", f, Sw(0, "press", ts=1.0))
    map_event("VENUES", f, Enc(0, 1, ts=1.1))
    assert map_event("VENUES", f, Sw(0, "short", ts=1.2)) == []


def test_b1_leaves_and_the_page_comes_home() -> None:
    assert map_event("VENUES", vf(), Sw(4, "press")) == [im.NextPage()]
    assert map_event("VENUES", vf(), Sw(4, "long")) == [im.GotoPage("SHOW")]
    assert "VENUES" in im.AUTO_HOME_PAGES and "VENUES" not in im.PAGES
    assert map_event("VENUES", vf(), Sw(5, "long")) == [im.Mute("toggle")]     # B2 still mutes


def test_venues_is_a_setup_page_item() -> None:
    item = next(i for i in im.SETUP_ITEMS if i.field == "VENUES")
    assert item.kind == "page" and item.label == "Venues" and not item.needs_confirm
    f = Focus(item=im.SETUP_ITEMS.index(item))
    assert map_event("SETUP", f, Sw(0, "short")) == [im.GotoPage("VENUES")]


def test_controller_venue_ui_timers(tmp_path: Path) -> None:
    ctl, _ = make(tmp_path)
    t = [100.0]
    ctl._ui_clock = lambda: t[0]
    ctl.goto_page("VENUES")
    ctl.focus.venue_mode = "rename"
    t[0] += 61
    ctl.ui_tick()
    assert ctl.page == "SHOW" and ctl.focus.venue_mode == "list"


# ======================================================================= the screen
def _render(state: Any, status: Any, focus: Any) -> Any:
    img = pages.render("VENUES", state, status, focus, now=0.0)
    assert img.size == (480, 320)
    return img


def test_venues_page_renders_every_mode_and_garbage() -> None:
    rows = [("HALL", 1760000000.0), None, ("A VERY LONG NAME X", 0.0)] + [None] * 5
    st = pages.ScreenStatus(device_present=True, engine="Running", venues=rows, venue_slot=0,
                            venue_name="HALL", venue_undo_s=120.0,
                            venue_changes=["Sub delay 0.00 → 2.90 ms"] * 9)
    for mode in ("list", "menu", "rename"):
        for sel in range(9):
            for menu in range(4):
                _render(State(), st, Focus(venue=sel, venue_mode=mode, venue_menu=menu,
                                           venue_buf=list("ABC"), venue_cursor=menu * 5))
    bad = pages.ScreenStatus(venues=["x", (None, "y"), 5, ("", 1.0)], venue_slot="no",
                             venue_changes=None, venue_undo_s=float("nan"))
    _render(None, bad, Focus(venue=99, venue_mode="menu", venue_menu=-3, venue_cursor="z"))
    _render(object(), object(), object())


def test_recall_list_shows_every_change_on_the_box() -> None:
    """F3: 7 compact lines are all drawn (the last one in the room of "master and mute stay"),
    the MUTE FIRST and the louder lines orange."""
    rows = [("HALL", 1760000000.0)] + [None] * 7
    lines = ["Mono subs off → ON · MUTE FIRST", "Sub delay 0.00 → 2.90 ms", "Top delay 0.00 → 0.75 ms",
             "Polarity → sub INV · top INV", "Lo-Cut 100 → 90 Hz", "Balance 0.0 → +4.0 dB (subs +2)",
             "EQ: live 2 · system 1"]
    st = pages.ScreenStatus(device_present=True, engine="Running", venues=rows, venue_changes=lines)
    img = _render(State(), st, Focus(venue=0, venue_mode="menu", venue_menu=0))
    top, pitch, x0 = pages.STATUS_H + 96 + 24, 20, 206

    def colours(k: int) -> set[Any]:
        return {img.getpixel((x, y)) for x in range(x0, 470) for y in range(top + k * pitch + 2,
                                                                          top + k * pitch + 14)}
    assert pages.ORANGE in colours(0)                     # MUTE FIRST
    assert pages.FG in colours(1) and pages.ORANGE not in colours(1)
    assert pages.ORANGE in colours(5)                     # the subs rise
    assert pages.FG in colours(6)                         # the 7th line is drawn
    assert top + 6 * pitch + 18 < pages.H - pages.FOOTER_H
    assert pages.venue_line_colour("Live EQ: 2 bands (1 louder)") == pages.ORANGE
    assert pages.venue_line_colour("Lo-Cut ON → OFF") == pages.ORANGE
    assert pages.venue_line_colour("Live EQ: 2 bands") == pages.FG


def test_venues_page_selected_row_and_confirm_colours() -> None:
    rows = [("HALL", 1760000000.0)] + [None] * 7
    st = pages.ScreenStatus(device_present=True, engine="Running", venues=rows)
    img = _render(State(), st, Focus(venue=0))
    assert img.getpixel((300, pages.STATUS_H + 30 + 2)) == pages.AMBER       # selected row
    f = Focus(venue=0, venue_mode="menu", venue_menu=0, confirm="venue_recall:0",
              confirm_until=math.inf)
    img = _render(State(), st, f)
    assert img.getpixel((100, pages.STATUS_H + 92 + 2)) == pages.RED          # PUSH AGAIN
    # bound to the saved time: the same key as the input map's
    g = Focus(venue=0, venue_mode="menu", venue_menu=0, venue_stamps=(1760000000.0,),
              confirm=im.venue_confirm_key(Focus(venue_stamps=(1760000000.0,)), "recall", 0),
              confirm_until=math.inf)
    assert _render(State(), st, g).getpixel((100, pages.STATUS_H + 92 + 2)) == pages.RED
    g.venue_stamps = (1760000999.0,)                    # saved again meanwhile: not pending
    assert _render(State(), st, g).getpixel((100, pages.STATUS_H + 92 + 2)) == pages.AMBER
    # F4: the Undo recall row waiting for its second push is red
    u = Focus(venue=im.VENUE_UNDO_ROW, venue_undo_ok=True, confirm=im.VENUE_UNDO_KEY,
              confirm_until=math.inf)
    st_u = pages.ScreenStatus(device_present=True, engine="Running", venues=rows, venue_undo_s=200.0)
    img = _render(State(), st_u, u)
    assert pages.RED in {img.getpixel((x, y)) for x in range(10, 400, 7) for y in range(40, 290, 3)}
    muted = pages.render("VENUES", State(muted=True), pages.ScreenStatus(
        device_present=True, engine="Running", muted=True, venues=rows), Focus(), now=0.0)
    assert muted.getpixel((150, pages.H - 8)) == pages.BAND_RED                # red footer
    assert muted.getpixel((2, 100)) == pages.BAND_RED                           # frame


def test_venue_pill_on_show_is_neutral_and_folds_first() -> None:
    assert not pages._risky_pill("VENUE: HALL")
    shown, folded, _ = pages.pill_layout(["LO-CUT OFF", "MONO SUB", "VENUE: SALLE DES FETES"], 220)
    assert "VENUE: SALLE DES FETES" in folded and "LO-CUT OFF" in shown


def test_demo_cases_include_the_venue_screens() -> None:
    names = {c[0] for c in pages.demo_cases()}
    assert {"venues", "venues_open", "venues_recall_confirm", "venues_overwrite_confirm",
            "venues_empty", "venues_rename", "venues_muted", "show_venue_pill",
            "setup_venues", "venues_open_many", "venues_undo_confirm"} <= names


# ======================================================================= the phone
def test_bridge_venue_messages_and_validation(tmp_path: Path) -> None:
    async def body() -> None:
        target = venue_state()
        write_venues(tmp_path / "venues.json", [None, slot_dict(target, "Chapel", 1760000000.0)])
        ctl, cl = make(tmp_path, playing=True)
        await boot(ctl)
        b = PhoneBridge(ctl)
        bad: list[dict[str, Any]] = [
            {"t": "venue_recall", "s": 1}, {"t": "venue_recall", "s": 1, "confirm": "yes"},
            {"t": "venue_recall", "s": True, "confirm": True}, {"t": "venue_recall", "s": -1, "confirm": True},
            {"t": "venue_recall", "s": 8, "confirm": True}, {"t": "venue_recall", "s": "1", "confirm": True},
            {"t": "venue_recall", "s": 1.0, "confirm": True}, {"t": "venue_recall", "s": 0, "confirm": True},
            {"t": "venue_recall", "s": 1, "confirm": True, "at": "now"},
            {"t": "venue_recall", "s": 1, "confirm": True, "at": 5.0},
            {"t": "venue_diff", "s": 0}, {"t": "venue_diff"},
            {"t": "venue_rename", "s": 1}, {"t": "venue_rename", "s": 1, "name": 7},
            {"t": "venue_rename", "s": 1, "name": "x" * 65}, {"t": "venue_rename", "s": 1, "name": "?!"},
            {"t": "venue_rename", "s": 0, "name": "ok"},
            {"t": "venue_save", "s": 1, "name": "over"},              # occupied: needs confirm
            {"t": "venue_save", "s": 2, "name": ["x"]}, {"t": "venue_save", "s": 2, "name": "%%"},
            {"t": "venue_undo", "confirm": True}, {"t": "venue_undo"},
        ]
        before = ctl.state.to_dict()
        for m in bad:
            ok, v, why = b.apply(m)
            assert not ok and why, m
        assert ctl.state.to_dict() == before and ctl.venues.get(2) is None
        assert ctl.venues.get(1).name == "CHAPEL"            # type: ignore[union-attr]
        ok, v, _ = b.apply({"t": "venue_diff", "s": 1})
        assert ok and v["n"] == "CHAPEL" and v["at"] == 1760000000.0 and v["muted"] is False
        assert v["changes"][0] == "Sub delay 0.00 → 2.90 ms"
        mark = len(cl.log)
        ok, v, why = b.apply({"t": "venue_recall", "s": 1, "confirm": True, "at": 1760000000.0})
        assert ok and v == "RECALLED CHAPEL · UNDO IN VENUES", why
        assert venue_part(ctl.state) == vn.values_of(target)
        assert await ctl.settle(timeout=5.0)
        assert not [e for e in cl.log[mark:] if e[0] in ("fader", "mute") and e[1] == 0]
        assert b.apply({"t": "venue_undo", "confirm": True}) == (True, "RECALL UNDONE", "")
        assert venue_part(ctl.state) == venue_part(State())
        ok, v, _ = b.apply({"t": "venue_save", "s": 2, "name": "Église St-Jean"})
        assert ok and v == "SAVED IN SLOT 3 · EGLISE ST-JEAN"
        assert b.apply({"t": "venue_save", "s": 2, "name": "x", "confirm": True})[0]
        assert ctl.venues.get(2).name == "X"                  # type: ignore[union-attr]
        assert b.apply({"t": "venue_rename", "s": 2, "name": "garden"})[1] == "SLOT 3 IS NOW GARDEN"
        await ctl.stop()
        assert [v.name if v else None for v in vn.VenueStore(tmp_path / "venues.json").slots][:3] == \
            [None, "CHAPEL", "GARDEN"]

    run(body())


def test_state_message_carries_the_slots_the_pill_and_the_undo(tmp_path: Path) -> None:
    write_venues(tmp_path / "venues.json", [slot_dict(venue_state(), "Hall", 1760000000.0)])
    ctl, _ = make(tmp_path)
    m = PhoneBridge.state_message(ctl.snapshot(), phone_on=True, phones=1)
    json.dumps(m, allow_nan=False)
    assert m["venues"][0] == {"n": "HALL", "at": 1760000000.0} and m["venues"][1:] == [None] * 7
    assert m["venue"] is None and m["venue_undo_s"] == 0
    t = [10.0]
    ctl._ui_clock = lambda: t[0]
    ctl.recall_venue(0)
    t[0] += 3.0                                      # 297 s left -> sent in 5 s steps: 300
    m = PhoneBridge.state_message(ctl.snapshot(), phone_on=True, phones=1)
    assert m["venue"] == {"s": 0, "n": "HALL"} and m["venue_undo_s"] == 300
    t[0] += 4.0
    assert PhoneBridge.state_message(ctl.snapshot(), phone_on=True, phones=1)["venue_undo_s"] == 295
    old = PhoneBridge.state_message(("SHOW", State(), object(), None), phone_on=True, phones=0)
    assert old["venues"] == [None] * 8 and old["venue"] is None      # an older status object
    assert PhoneBridge(ctl).hello(True)["venues"] == {"slots": 8, "name_max": 16,
                                                      "name_chars": vn.NAME_PICKER}


def test_venue_messages_over_the_websocket(tmp_path: Path) -> None:
    """Through the server's validated dispatch: acks, refusals, the shared rate bucket, and every
    phone seeing the new slot at once."""
    async def body() -> None:
        ctl, _ = make(tmp_path, venue_state(), playing=True)
        await boot(ctl)
        srv = PhoneServer(ctl, settings(tmp_path, rate_per_s=5.0, burst=6), tick_s=0.02)
        await srv.start()
        url = f"ws://127.0.0.1:{srv.ports['plain']}/ws"
        async with ws_connect(url, origin=f"http://127.0.0.1:{srv.ports['plain']}") as ws:
            hello = json.loads(await ws.recv())
            assert hello["venues"]["slots"] == 8
            await recv_until(ws, lambda m: m["t"] == "state")
            await ws.send(json.dumps({"t": "venue_save", "s": 4, "name": "Market hall", "id": "vsave"}))
            ack = await recv_until(ws, lambda m: m.get("id") == "vsave")
            assert ack == {"t": "ack", "id": "vsave", "v": "SAVED IN SLOT 5 · MARKET HALL"}
            st = await recv_until(ws, lambda m: m["t"] == "state" and m["venues"][4] is not None)
            assert st["venues"][4]["n"] == "MARKET HALL" and st["venue"] == {"s": 4, "n": "MARKET HALL"}
            await ws.send(json.dumps({"t": "venue_save", "s": 4, "name": "x", "id": "v2"}))
            err = await recv_until(ws, lambda m: m.get("id") == "v2")
            assert err["t"] == "err" and "confirm" in err["why"]
            await ws.send(json.dumps({"t": "venue_recall", "s": 9, "confirm": True, "id": "v3"}))
            err = await recv_until(ws, lambda m: m.get("id") == "v3")
            assert err["t"] == "err" and "0..7" in err["why"]
            # the same bucket as every other write: a burst gets "too fast"
            for k in range(10):
                await ws.send(json.dumps({"t": "venue_diff", "s": 4, "id": f"d{k}"}))
            first = await recv_until(ws, lambda m: m.get("id") == "d0")
            assert first["t"] == "ack" and first["v"]["changes"] == [] and first["v"]["n"] == "MARKET HALL"
            await until(lambda: srv.rx.get("venue_diff") == 10, what="all received")
            assert srv.stats["rate_limited"] >= 1
            assert srv.rx["venue_save"] == 2
        await srv.stop()
        await ctl.stop()

    run(body())
