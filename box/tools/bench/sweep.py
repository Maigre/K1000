#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Stepped sine response of the loop, run ON the box: each tone on the HiFiBerry (-20 dBFS),
return minus source RMS from the engine meters (capture 3 - capture 2). Usage: sweep.py [Hz ...]"""
import json, math, subprocess, sys, time, wave
import numpy as np
import websockets.sync.client as c

FS = 48000
freqs = [float(a) for a in sys.argv[1:]] or [20, 25, 31.5, 40, 50, 63, 80, 100, 200, 1000, 5000, 10000, 16000, 20000]
w = c.connect("ws://127.0.0.1:1234")


def rms(cmd, s):
    w.send(json.dumps({"command": cmd, "value": s}))
    return json.loads(w.recv())["value"]


ref = None
for f in freqs:
    t = np.arange(int(3.2 * FS)) / FS
    x = 10 ** (-20 / 20) * math.sqrt(2) * np.sin(2 * np.pi * f * t)
    x *= np.minimum(1, np.minimum(t, t[-1] - t) / 0.05)
    i32 = np.repeat((x * (2**31 - 1)).astype("<i4")[:, None], 2, axis=1)
    path = "/tmp/k1000-m4loop/tone.wav"
    with wave.open(path, "wb") as wf:
        wf.setnchannels(2); wf.setsampwidth(4); wf.setframerate(FS); wf.writeframes(i32.tobytes())
    p = subprocess.Popen(["aplay", "-q", "-D", "hw:sndrpihifiberry", path])
    time.sleep(2.7)
    cap = rms("GetCaptureSignalRmsSince", 1.5)
    pb = rms("GetPlaybackSignalRmsSince", 1.5)
    p.wait()
    g = cap[3] - cap[2]
    ref = g if f == 1000 else ref
    print(f"{f:8.1f} Hz  src {cap[2]:7.2f}  ret {cap[3]:7.2f}  ret-src {g:7.2f}  pb {[round(v, 1) for v in pb]}")
