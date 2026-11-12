#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Produced-fault test: freeze the engine for a moment, then see whether its delay comes back.

Run ON the box, as the user the engine runs as (it signals the engine process). Speakers off:
each freeze is an audible gap.

What it does: reads the engine's playback buffer level, stops the camilladsp process for --ms
milliseconds (SIGSTOP, then SIGCONT), and reads the level again once the engine has published a
new average. A healthy engine is back at its level from before. An engine that keeps the backlog
(CamillaDSP 5.0.0-beta.1 with rate adjust off, docs/16 §2.2) stays higher, and higher again
after each freeze.

The buffer level shows only the playback side (device buffer + the queue in front of it). The
whole in-to-out delay is what `m4loop.py` measures through the loop cable: run it before and
after this script.

Changes nothing in K1000's settings. The engine keeps whatever delay the freezes left until it
is restarted.
"""
import argparse, json, os, re, signal, subprocess, sys, time
import websockets.sync.client as wsc

LIVE_LOG = "/var/log/k1000/camilladsp_rCURRENT.log"
DROPOUT = re.compile(rb"underrun|overrun|xrun|stalled|PlaybackError|CaptureError", re.IGNORECASE)


class Engine:
    def __init__(self, url):
        self.url = url
        self.w = wsc.connect(url, max_size=None)

    def cmd(self, name, value=None):
        msg = {"command": name} if value is None else {"command": name, "value": value}
        self.w.send(json.dumps(msg))
        r = json.loads(self.w.recv())
        if r.get("result") != "Ok":
            raise RuntimeError(f"{name}: {r}")
        return r.get("value")


def engine_pid():
    out = subprocess.run(["systemctl", "show", "-p", "MainPID", "--value", "camilladsp"],
                         capture_output=True, text=True).stdout.strip()
    pid = int(out) if out.isdigit() else 0
    if pid <= 0:
        sys.exit("camilladsp.service has no main process")
    with open(f"/proc/{pid}/comm") as f:
        if f.read().strip() != "camilladsp":
            sys.exit(f"pid {pid} is not camilladsp (the unit is still waiting for a config or a device)")
    return pid


def dropout_lines(path=LIVE_LOG):
    try:
        with open(path, "rb") as f:
            return [l.decode("utf-8", "replace").rstrip() for l in f if DROPOUT.search(l)]
    except OSError:
        return None


def level(e, wait_s):
    """The level once the engine has published an average taken after `wait_s` seconds: it
    refreshes the figure every adjust period (10 s by default), so a read right after an event
    still shows the old one."""
    time.sleep(wait_s)
    return int(e.cmd("GetBufferLevel"))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--ms", type=float, default=50.0, help="length of each freeze (default 50)")
    ap.add_argument("--n", type=int, default=1, help="number of freezes (default 1)")
    ap.add_argument("--settle", type=float, default=12.0,
                    help="seconds to wait before each level read (default 12: one adjust period + margin)")
    ap.add_argument("--url", default="ws://127.0.0.1:1234")
    a = ap.parse_args()

    e = Engine(a.url)
    cfg = json.loads(e.cmd("GetConfigJson"))["devices"]
    fs, chunk = int(cfg["samplerate"]), int(cfg["chunksize"])
    res = {"version": e.cmd("GetVersion"), "samplerate": fs, "chunksize": chunk,
           "target_level": cfg.get("target_level"), "rate_adjust": bool(cfg.get("enable_rate_adjust")),
           "freeze_ms": a.ms, "freezes": []}
    pid = engine_pid()
    before_log = dropout_lines()
    base = level(e, a.settle)
    res["level_before"] = base
    for i in range(a.n):
        t0 = time.monotonic()
        os.kill(pid, signal.SIGSTOP)
        try:
            time.sleep(a.ms / 1000.0)
        finally:
            os.kill(pid, signal.SIGCONT)
        frozen = (time.monotonic() - t0) * 1000.0
        try:
            lv = level(e, a.settle)
        except Exception:                                  # the engine ended (it exits on some errors)
            res["freezes"].append({"n": i + 1, "frozen_ms": round(frozen, 1), "engine": "gone"})
            break
        res["freezes"].append({"n": i + 1, "frozen_ms": round(frozen, 1), "level": lv,
                               "above_before_frames": lv - base,
                               "above_before_ms": round((lv - base) / fs * 1000.0, 1),
                               "state": e.cmd("GetState")})
    after_log = dropout_lines()
    if before_log is not None and after_log is not None:
        res["new_dropout_lines"] = after_log[len(before_log):][-12:]
    last = res["freezes"][-1] if res["freezes"] else {}
    if "level" in last:
        res["came_back"] = last["above_before_frames"] <= chunk
    print(json.dumps(res, indent=1))
    return 0 if res.get("came_back") else 1


if __name__ == "__main__":
    sys.exit(main())
