#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Play the 1 kHz test tone on the HiFiBerry (unless --ext) and print capture + playback RMS
(dBFS, 2 s), run ON the box. Optional label as the first argument."""
import json, subprocess, sys, time
import websockets.sync.client as c

ext = "--ext" in sys.argv
p = None if ext else subprocess.Popen(["aplay", "-q", "-D", "hw:sndrpihifiberry", "-d", "4",
                                       "/tmp/k1000-m4loop/sine1k.wav"])
time.sleep(2.6)
w = c.connect("ws://127.0.0.1:1234")


def g(n):
    w.send(json.dumps({"command": n, "value": 2.0}))
    return [round(x, 2) for x in json.loads(w.recv())["value"]]


cap, pb = g("GetCaptureSignalRmsSince"), g("GetPlaybackSignalRmsSince")
if p:
    p.wait()
label = next((a for a in sys.argv[1:] if not a.startswith("--")), "")
print(label, "capture", cap, "playback", pb, "ret-src", round(cap[3] - cap[2], 2))
