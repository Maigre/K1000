#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Start series: restart the engine N times and measure the in-to-out delay after each start.

Run ON the box with the loop cable in (see m4loop.py) and nothing else on the outputs: every
start is a gap, and every measurement plays a tone and pink noise. Needs sudo for the restart.

The delay is not the same after every start of the audio stream, and the engine's buffer level
does not always show the difference. This prints one JSON line per start: the delay, the
level, and the spread at the end.
"""
import argparse, json, os, statistics, subprocess, sys, time
import websockets.sync.client as wsc

URL = "ws://127.0.0.1:1234"
HERE = os.path.dirname(os.path.abspath(__file__))


def ask(w, name, value=None):
    w.send(json.dumps({"command": name} if value is None else {"command": name, "value": value}))
    r = json.loads(w.recv())
    return r.get("value")


def wait_ramped(timeout=45.0):
    """Wait until the engine runs and K1000 has brought Main back (unmuted, level steady for 2 s)."""
    t0 = time.monotonic()
    last, since = None, None
    while time.monotonic() - t0 < timeout:
        try:
            with wsc.connect(URL, max_size=None, open_timeout=2) as w:
                if ask(w, "GetState") == "Running":
                    f = ask(w, "GetFaders")[0]
                    cur = (round(float(f["volume"]), 2), bool(f["mute"]))
                    if cur != last:
                        last, since = cur, time.monotonic()
                    elif not cur[1] and time.monotonic() - since >= 2.0:
                        return cur[0]
        except Exception:
            last, since = None, None
        time.sleep(0.25)
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out", type=int, help="the M4 output looped back (1-4)")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--settle", type=float, default=12.0, help="seconds after the ramp before measuring")
    ap.add_argument("--unit", default="camilladsp.service")
    ap.add_argument("--no-restart", action="store_true", help="measure N times without restarting (repeatability)")
    a = ap.parse_args()
    lat = []
    for i in range(a.n):
        row = {"n": i + 1}
        if not a.no_restart:
            subprocess.run(["sudo", "systemctl", "restart", a.unit], check=True)
            main_db = wait_ramped()
            row["main_db"] = main_db
            if main_db is None:
                row["error"] = "the engine did not come back running and unmuted"
                print(json.dumps(row), flush=True)
                continue
            time.sleep(a.settle)
        r = subprocess.run([sys.executable, os.path.join(HERE, "m4loop.py"), str(a.out), "--avg", "4"],
                           capture_output=True, text=True)
        try:
            j = json.loads(r.stdout)
        except ValueError:
            row["error"] = (r.stderr or r.stdout)[-200:]
            print(json.dumps(row), flush=True)
            continue
        row.update(latency_ms=j["latency_ms"], snr=j["comb_snr"], level=j["buffer_level_frames"],
                   loop_gain_db=j["analog_loop_gain_db"])
        if j.get("latency_warning"):
            row["warning"] = j["latency_warning"]
        else:
            lat.append(j["latency_ms"])
        print(json.dumps(row), flush=True)
    if lat:
        print(json.dumps({"starts": len(lat), "min_ms": min(lat), "max_ms": max(lat),
                          "median_ms": round(statistics.median(lat), 3),
                          "spread_ms": round(max(lat) - min(lat), 3)}), flush=True)


if __name__ == "__main__":
    main()
