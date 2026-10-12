#!/bin/sh
# k1000-soak: every 10 s, append one line about the engine's health to /var/log/k1000/soak.log.
#
# For the overnight soak (plan: Thu 15) and the 2-hour program run (test protocol step 8): leave
# it running, then read the log in the morning. One key=value line per sample, greppable:
#
#   2026-10-15T23:00:10+02:00 state=Running load=1.23 buffer=0 clipped=0 xruns=0 xruns_all=0 rate=48000 restarts=0 temp_c=52.1 throttled=0x0
#
#   state      CamillaDSP processing state (Running/Paused/Inactive/Starting/Stalled), DOWN = no websocket
#   load       processing load, % of the real-time budget
#   buffer     playback buffer level in frames (CamillaDSP reports 0 when rate adjust is off, as in v0)
#   clipped    clipped samples since the engine started (or since a ResetClippedSamples)
#   xruns      dropout lines in the live engine log: 'underrun|overrun|xrun|stalled|PlaybackError|
#              CaptureError', any case (playback underruns, capture overruns such as a USB hiccup on
#              the bus-powered M4, stalled devices, device errors)
#   xruns_all  the same over the live log + the rotated ones kept (they survive engine restarts)
#   rate       measured capture rate, Hz (a drifting clock shows here first)
#   restarts   systemd's restart counter for camilladsp.service (a crash loop shows here)
#   temp_c     SoC temperature (the box lives in a case); throttled = `vcgencmd get_throttled`
#              (bit 0 = under-voltage now: the bus-powered M4 draws from the Pi's USB)
#
# Usage:   k1000-soak.sh [--once]
#   run it detached, as k1000, so it survives the SSH session:
#     sudo systemd-run --unit=k1000-soak --uid=k1000 /opt/k1000/tools/k1000-soak.sh
#     sudo systemctl stop k1000-soak          # when done
#   live view:  tail -f /var/log/k1000/soak.log
# Env (for tests): K1000_WS (ws://127.0.0.1:1234), K1000_SOAK_LOG (/var/log/k1000/soak.log),
#   K1000_CDSP_LOG (/var/log/k1000/camilladsp.log, the name given to camilladsp -o),
#   K1000_SOAK_INTERVAL (10), K1000_SOAK_UNIT (camilladsp.service).
#
# Why one long-lived python3 process instead of a client per sample: starting python + websockets
# every 10 s on the Pi would itself disturb the load figures being measured.
set -eu

ONCE=0
[ "${1:-}" = "--once" ] && ONCE=1
export K1000_SOAK_ONCE=$ONCE

exec python3 -I - <<'PY'
import glob, json, os, re, subprocess, sys, time
from datetime import datetime

from websockets.sync.client import connect

WS = os.environ.get("K1000_WS", "ws://127.0.0.1:1234")
OUT = os.environ.get("K1000_SOAK_LOG", "/var/log/k1000/soak.log")
CDSP_LOG = os.environ.get("K1000_CDSP_LOG", "/var/log/k1000/camilladsp.log")
INTERVAL = float(os.environ.get("K1000_SOAK_INTERVAL", "10"))
UNIT = os.environ.get("K1000_SOAK_UNIT", "camilladsp.service")
ONCE = os.environ.get("K1000_SOAK_ONCE") == "1"
# Matches CamillaDSP v5's own wording ("PB: write underrun", "Capture: read overrun", "Capture
# device is stalled", "Entering stalled state", "PlaybackError"...): a capture-side dropout must
# count too, or a soak with USB hiccups would report zero.
PATTERN = re.compile(rb"underrun|overrun|xrun|stalled|PlaybackError|CaptureError", re.IGNORECASE)

# camilladsp -o X.log with --log_rotate_size writes X_rCURRENT.log and keeps X_r<date>.log files.
stem, ext = os.path.splitext(CDSP_LOG)
LIVE_LOG = f"{stem}_rCURRENT{ext}"
ALL_LOGS = f"{glob.escape(stem)}_r*{ext}"

ws = None


def command(name):
    """One v5 request/response: {"command": X} -> {"reply": X, "result": "Ok", "value": ...}."""
    ws.send(json.dumps({"command": name}))
    reply = json.loads(ws.recv(timeout=3))
    if reply.get("result") != "Ok":
        return "ERR:" + str(reply.get("result"))
    return reply.get("value")


def engine():
    """Engine fields; reconnects lazily, so an engine restart costs one 'DOWN' sample."""
    global ws
    names = ("GetState", "GetProcessingLoad", "GetBufferLevel", "GetClippedSamples", "GetCaptureRate")
    for attempt in (1, 2):
        try:
            if ws is None:
                ws = connect(WS, open_timeout=3, close_timeout=1)
            return [command(n) for n in names]
        except Exception:
            try:
                if ws is not None:
                    ws.close()
            except Exception:
                pass
            ws = None
    return ["DOWN", None, None, None, None]


def count(path):
    try:
        with open(path, "rb") as f:
            return sum(1 for line in f if PATTERN.search(line))
    except OSError:
        return None


def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def temp_c():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return f"{int(f.read()) / 1000:.1f}"
    except (OSError, ValueError):
        return None


def fmt(v):
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def sample():
    state, load, buffer, clipped, rate = engine()
    counts = [count(p) for p in sorted(glob.glob(ALL_LOGS))]
    restarts = run(["systemctl", "show", "-p", "NRestarts", "--value", UNIT]) or None
    throttled = run(["vcgencmd", "get_throttled"]).partition("=")[2] or None
    fields = [
        ("state", state), ("load", load), ("buffer", buffer), ("clipped", clipped),
        ("xruns", count(LIVE_LOG)), ("xruns_all", sum(c for c in counts if c is not None) if counts else None),
        ("rate", rate), ("restarts", restarts), ("temp_c", temp_c()), ("throttled", throttled),
    ]
    ts = datetime.now().astimezone().isoformat(timespec="seconds")
    line = ts + " " + " ".join(f"{k}={fmt(v)}" for k, v in fields) + "\n"
    with open(OUT, "a") as f:
        f.write(line)


next_t = time.monotonic()
while True:
    sample()
    if ONCE:
        break
    next_t += INTERVAL
    time.sleep(max(0.0, next_t - time.monotonic()))
PY
