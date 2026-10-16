#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Set Mono L+R on/off the way a CamillaGUI edit does (run ON the box): the running config's
split mixer gets the stereo or mono sub mapping; k1000 adopts it (dspconfig.state_from_config:
mono_subs from the number of Sub L sources) and saves it. Usage: monosubs.py on|off"""
import json, sys
import websockets.sync.client as wsc

want = sys.argv[1] == "on"
w = wsc.connect("ws://127.0.0.1:1234", max_size=None)


def cmd(name, value=None):
    w.send(json.dumps({"command": name} if value is None else {"command": name, "value": value}))
    r = json.loads(w.recv())
    assert r.get("result") == "Ok", r
    return r.get("value")


cfg = json.loads(cmd("GetConfigJson"))
sp = cfg["mixers"]["split"]


def src(c, g):
    return {"channel": c, "gain": g, "inverted": False, "mute": False, "scale": "dB"}


subs = ([{"dest": d, "mute": False, "sources": [src(0, -6.0), src(1, -6.0)]} for d in (0, 1)] if want
        else [{"dest": d, "mute": False, "sources": [src(d, 0.0)]} for d in (0, 1)])
sp["mapping"] = subs + [m for m in sp["mapping"] if m["dest"] in (2, 3)]
cmd("SetConfigJson", json.dumps(cfg))
now = json.loads(cmd("GetConfigJson"))["mixers"]["split"]["mapping"]
print("split now:", [(m["dest"], [s["channel"] for s in m["sources"]]) for m in now])
