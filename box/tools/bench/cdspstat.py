#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only engine status (CamillaDSP v5 websocket), run ON the box."""
import json, sys
import websockets.sync.client as c

w = c.connect("ws://127.0.0.1:1234")
CMDS = [("GetState", None), ("GetCaptureRate", None), ("GetBufferLevel", None),
        ("GetProcessingLoad", None), ("GetFaders", None), ("GetStopReason", None),
        ("GetCaptureSignalPeakSince", 2.0), ("GetPlaybackSignalPeakSince", 2.0),
        ("GetCaptureSignalRmsSince", 2.0)]
for n, v in CMDS:
    msg = {"command": n} if v is None else {"command": n, "value": v}
    w.send(json.dumps(msg))
    r = json.loads(w.recv())
    val = r.get("value", r)
    if isinstance(val, list) and val and isinstance(val[0], (int, float)):
        val = [round(x, 1) for x in val]
    print(n, json.dumps(val)[:220], r.get("result", ""))
