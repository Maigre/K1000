#!/bin/sh
# k1000-soak: every 10 s, append one line about the box's health to /var/log/k1000/soak.log.
#
# For the overnight soak (plan: Thu 15) and the 2-hour program run (test protocol step 8): leave
# it running, then read the summary in the morning. One key=value line per sample, greppable:
#
#   2026-10-15T23:00:10+02:00 state=Running load=1.23 buffer=224 clipped=0 xruns=0 xruns_all=0 rate=48000 restarts=0 temp_c=52.1 throttled=0x0 main_db=-20.0 muted=false k1000=active k1000_restarts=0 boot=3f2a9c1e up=8123
#
#   state      CamillaDSP processing state (Running/Paused/Inactive/Starting/Stalled), DOWN = no websocket
#   load       processing load, % of the real-time budget
#   buffer     playback buffer level in frames
#   clipped    clipped samples since the engine started (or since a ResetClippedSamples)
#   xruns      dropout lines in the live engine log: 'underrun|overrun|xrun|stalled|PlaybackError|
#              CaptureError', any case (playback underruns, capture overruns such as a USB hiccup on
#              the bus-powered M4, stalled devices, device errors)
#   xruns_all  the same over the live log + the rotated ones kept (they survive engine restarts)
#   rate       measured capture rate, Hz (a drifting clock shows here first)
#   restarts   systemd's restart counter for camilladsp.service (a crash loop shows here)
#   temp_c     SoC temperature (the box lives in a case); throttled = `vcgencmd get_throttled`
#              (bit 0 = under-voltage now: the bus-powered M4 draws from the Pi's USB)
#   main_db, muted   the engine's Main fader (what the PA gets), from GetFaders
#   k1000, k1000_restarts   k1000.service's state and systemd restart counter (knobs + screen)
#   boot, up   first 8 characters of the kernel boot id, and uptime in seconds: a new boot= value
#              (or `up` going down) means the box rebooted during the soak
#
# Usage:   k1000-soak.sh [--once]          sample (every 10 s, or once)
#          k1000-soak.sh --summary [LOG]   read a soak log: samples, gaps, reboots, xruns,
#                                          restarts, mute, heat; exits 1 if anything needs a look
#   run it detached, as k1000, so it survives the SSH session:
#     sudo systemd-run --unit=k1000-soak --uid=k1000 /opt/k1000/tools/k1000-soak.sh
#     sudo systemctl stop k1000-soak          # when done ("not loaded" = it had ended: a reboot?)
#   live view:  tail -f /var/log/k1000/soak.log
#   A transient systemd-run unit does not survive a reboot: the log then just stops. --summary
#   says so (last sample long before now, or a gap), and `journalctl --list-boots` shows it.
# Env (for tests): K1000_WS (ws://127.0.0.1:1234), K1000_SOAK_LOG (/var/log/k1000/soak.log),
#   K1000_CDSP_LOG (/var/log/k1000/camilladsp.log, the name given to camilladsp -o),
#   K1000_SOAK_INTERVAL (10), K1000_SOAK_UNIT (camilladsp.service), K1000_SOAK_K1000_UNIT
#   (k1000.service), K1000_SOAK_BOOT_ID (/proc/sys/kernel/random/boot_id), K1000_SOAK_UPTIME
#   (/proc/uptime), K1000_SOAK_NOW (ISO time used as "now" by --summary).
#
# Why one long-lived python3 process instead of a client per sample: starting python + websockets
# every 10 s on the Pi would itself disturb the load figures being measured. Rotated engine logs
# never change, so their dropout counts are cached (by size and mtime) instead of re-read.
set -eu

MODE=run
case "${1:-}" in
  "") ;;
  --once) MODE=once ;;
  --summary)
    MODE=summary
    if [ -n "${2:-}" ]; then K1000_SOAK_LOG=$2; export K1000_SOAK_LOG; fi ;;
  -h|--help) sed -n '2,/^set -eu/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
  *) echo "k1000-soak: unknown option '$1' (try --help)" >&2; exit 2 ;;
esac
export K1000_SOAK_MODE="$MODE"

exec python3 -I - <<'PY'
import glob, json, os, re, subprocess, sys, time
from datetime import datetime

MODE = os.environ.get("K1000_SOAK_MODE", "run")
WS = os.environ.get("K1000_WS", "ws://127.0.0.1:1234")
OUT = os.environ.get("K1000_SOAK_LOG", "/var/log/k1000/soak.log")
CDSP_LOG = os.environ.get("K1000_CDSP_LOG", "/var/log/k1000/camilladsp.log")
INTERVAL = float(os.environ.get("K1000_SOAK_INTERVAL", "10"))
UNIT = os.environ.get("K1000_SOAK_UNIT", "camilladsp.service")
K1000_UNIT = os.environ.get("K1000_SOAK_K1000_UNIT", "k1000.service")
BOOT_ID = os.environ.get("K1000_SOAK_BOOT_ID", "/proc/sys/kernel/random/boot_id")
UPTIME = os.environ.get("K1000_SOAK_UPTIME", "/proc/uptime")
# Matches CamillaDSP v5's own wording ("PB: write underrun", "Capture: read overrun", "Capture
# device is stalled", "Entering stalled state", "PlaybackError"...): a capture-side dropout must
# count too, or a soak with USB hiccups would report zero.
PATTERN = re.compile(rb"underrun|overrun|xrun|stalled|PlaybackError|CaptureError", re.IGNORECASE)

# camilladsp -o X.log with --log_rotate_size writes X_rCURRENT.log and keeps X_r<date>.log files.
stem, ext = os.path.splitext(CDSP_LOG)
LIVE_LOG = f"{stem}_rCURRENT{ext}"
ALL_LOGS = f"{glob.escape(stem)}_r*{ext}"


# --------------------------------------------------------------------------- sampling
ws = None
_counts = {}        # path -> ((size, mtime_ns), dropout lines)


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
    from websockets.sync.client import connect
    names = ("GetState", "GetProcessingLoad", "GetBufferLevel", "GetClippedSamples", "GetCaptureRate",
             "GetFaders")
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
    return ["DOWN", None, None, None, None, None]


def main_fader(faders):
    """(volume dB, muted) of fader 0 (Main) in a GetFaders value; (None, None) if unusable."""
    try:
        vol, mute = faders[0]["volume"], faders[0]["mute"]
    except (TypeError, KeyError, IndexError):
        return None, None
    if isinstance(vol, bool) or not isinstance(vol, (int, float)) or not isinstance(mute, bool):
        return None, None
    return float(vol), mute


def count(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (st.st_size, st.st_mtime_ns)
    hit = _counts.get(path)
    if hit is not None and hit[0] == key:
        return hit[1]
    try:
        with open(path, "rb") as f:
            n = sum(1 for line in f if PATTERN.search(line))
    except OSError:
        return None
    _counts[path] = (key, n)
    return n


def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def unit_props(unit):
    """{'ActiveState': ..., 'NRestarts': ...} of a systemd unit ({} when systemctl fails)."""
    props = {}
    for line in run(["systemctl", "show", "-p", "ActiveState", "-p", "NRestarts", unit]).splitlines():
        k, sep, v = line.partition("=")
        if sep:
            props[k.strip()] = v.strip()
    return props


def read_first(path):
    try:
        with open(path) as f:
            return f.read().split()[0]
    except (OSError, IndexError):
        return None


def temp_c():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return f"{int(f.read()) / 1000:.1f}"
    except (OSError, ValueError):
        return None


def fmt(v):
    if v is None or v == "":
        return "-"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def sample():
    state, load, buffer, clipped, rate, faders = engine()
    main_db, muted = main_fader(faders)
    counts = [count(p) for p in sorted(glob.glob(ALL_LOGS))]
    for gone in [p for p in _counts if not os.path.exists(p)]:
        del _counts[gone]
    cdsp = unit_props(UNIT)
    k1000 = unit_props(K1000_UNIT)
    throttled = run(["vcgencmd", "get_throttled"]).partition("=")[2] or None
    boot = read_first(BOOT_ID)
    try:
        up = int(float(read_first(UPTIME)))
    except (TypeError, ValueError):
        up = None
    fields = [
        ("state", state), ("load", load), ("buffer", buffer), ("clipped", clipped),
        ("xruns", count(LIVE_LOG)), ("xruns_all", sum(c for c in counts if c is not None) if counts else None),
        ("rate", rate), ("restarts", cdsp.get("NRestarts")), ("temp_c", temp_c()), ("throttled", throttled),
        ("main_db", f"{main_db:.1f}" if main_db is not None else None), ("muted", muted),
        ("k1000", k1000.get("ActiveState")), ("k1000_restarts", k1000.get("NRestarts")),
        ("boot", boot[:8] if boot else None), ("up", up),
    ]
    ts = datetime.now().astimezone().isoformat(timespec="seconds")
    line = ts + " " + " ".join(f"{k}={fmt(v)}" for k, v in fields) + "\n"
    with open(OUT, "a") as f:
        f.write(line)


# --------------------------------------------------------------------------- summary
def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def hms(seconds):
    seconds = int(seconds)
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m{seconds % 60:02d}s"


def summary():
    """Read the soak log and say plainly what needs a look. Returns 1 if anything does."""
    rows = []
    try:
        with open(OUT) as f:
            for raw in f:
                parts = raw.split()
                if not parts:
                    continue
                try:
                    ts = datetime.fromisoformat(parts[0])
                except ValueError:
                    continue
                rows.append((ts, dict(p.split("=", 1) for p in parts[1:] if "=" in p)))
    except OSError as e:
        print(f"cannot read {OUT}: {e}")
        return 1
    if not rows:
        print(f"{OUT}: no samples")
        return 1
    issues = []
    now = os.environ.get("K1000_SOAK_NOW")
    now = datetime.fromisoformat(now) if now else datetime.now().astimezone()
    first, last = rows[0][0], rows[-1][0]
    print(f"{OUT}: {len(rows)} samples, {first.isoformat()} -> {last.isoformat()} "
          f"({hms((last - first).total_seconds())})")

    # A reboot (or a stopped soak) just ends the log: compare the last sample with now.
    age = (now - last).total_seconds()
    if age > max(60.0, 3 * INTERVAL):
        issues.append(f"last sample is {hms(age)} old: the soak is not running any more "
                      "(the box rebooted? `journalctl --list-boots`; or it was stopped)")
    gap_limit = max(30.0, 3 * INTERVAL)
    gaps = [(a[0], b[0]) for a, b in zip(rows, rows[1:]) if (b[0] - a[0]).total_seconds() > gap_limit]
    for a, b in gaps[:10]:
        issues.append(f"gap of {hms((b - a).total_seconds())} with no sample: {a.isoformat()} -> {b.isoformat()}")

    boots = []
    for ts, kv in rows:
        b = kv.get("boot")
        if b and b != "-" and (not boots or boots[-1][0] != b):
            boots.append((b, ts))
    if len(boots) > 1:
        issues.append("the box REBOOTED during the soak: "
                      + ", ".join(f"boot {b} from {ts.isoformat()}" for b, ts in boots))
    ups = [(ts, num(kv.get("up"))) for ts, kv in rows if num(kv.get("up")) is not None]
    for (_t0, u0), (t1, u1) in zip(ups, ups[1:]):
        if u1 < u0:
            issues.append(f"uptime went back ({int(u0)} s -> {int(u1)} s) at {t1.isoformat()}: a reboot")

    states = {}
    for _ts, kv in rows:
        st = kv.get("state", "-")
        states[st] = states.get(st, 0) + 1
    print("states: " + ", ".join(f"{k} {v}" for k, v in sorted(states.items(), key=lambda x: -x[1])))
    not_running = sum(v for k, v in states.items() if k != "Running")
    if not_running:
        issues.append(f"{not_running} samples not Running ({', '.join(k for k in states if k != 'Running')})")

    def series(key):
        return [(ts, num(kv.get(key))) for ts, kv in rows if num(kv.get(key)) is not None]

    for key in ("xruns", "xruns_all"):
        s = series(key)
        if s and max(v for _t, v in s) > 0:
            t_first = next(t for t, v in s if v > 0)
            issues.append(f"{key} reached {int(max(v for _t, v in s))} (first at {t_first.isoformat()}): "
                          "read the engine logs in /var/log/k1000")
    s = series("xruns_all")
    if any(b < a for (_t, a), (_t2, b) in zip(s, s[1:])):
        issues.append("xruns_all went DOWN: rotated engine logs were deleted (many engine restarts?)")
    # Restart counters start again from 0 at a reboot: add up the increases between samples.
    for key, what in (("restarts", "camilladsp.service"), ("k1000_restarts", "k1000.service")):
        s = series(key)
        n = sum(max(0.0, b - a) for (_t, a), (_t2, b) in zip(s, s[1:]))
        if n:
            issues.append(f"{what} restarted {int(n)} times during the soak")
    s = series("clipped")
    if s and max(v for _t, v in s) > 0:
        issues.append(f"clipped samples: up to {int(max(v for _t, v in s))}")
    k_down = [kv.get("k1000") for _ts, kv in rows if kv.get("k1000") not in (None, "-", "active")]
    if k_down:
        issues.append(f"k1000.service not active in {len(k_down)} samples ({', '.join(sorted(set(k_down)))})")
    muted = sum(1 for _ts, kv in rows if kv.get("muted") == "true")
    if muted:
        issues.append(f"Main muted in {muted} samples")
    m = series("main_db")
    if m:
        print(f"main_db: {min(v for _t, v in m):.1f} .. {max(v for _t, v in m):.1f} dB")
    r = series("rate")
    if r:
        print(f"rate: {min(v for _t, v in r):.0f} .. {max(v for _t, v in r):.0f} Hz")
    t = series("temp_c")
    if t:
        hot = max(v for _t, v in t)
        print(f"temp_c: max {hot:.1f}")
        if hot >= 75.0:
            issues.append(f"SoC reached {hot:.1f} C (the Pi 4 throttles from 80 C)")
    thr = sorted({kv.get("throttled") for _ts, kv in rows} - {None, "-", "0x0"})
    if thr:
        issues.append(f"throttled: {', '.join(thr)} (bit 0 = under-voltage now, bit 16 = since boot)")

    if issues:
        print(f"ATTENTION ({len(issues)}):")
        for i in issues:
            print(f"  - {i}")
        return 1
    print("clean: no gap, no reboot, no xrun, no restart, no clip, never muted, no throttling")
    return 0


if MODE == "summary":
    sys.exit(summary())

next_t = time.monotonic()
while True:
    sample()
    if MODE == "once":
        break
    next_t += INTERVAL
    time.sleep(max(0.0, next_t - time.monotonic()))
PY
