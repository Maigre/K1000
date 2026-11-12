#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Latency matrix: one engine binary, a list of chunk / target_level settings, measured on the box.

Run ON the box with the loop cable in (see m4loop.py) and nothing else on the outputs. It STOPS
camilladsp.service and k1000.service for the time of the run and starts them again at the end:
the engine runs under a transient unit of its own (same user, same real-time rights) on a copy
of the box's config with only `chunksize` and `target_level` changed, unmuted at 0 dB.

For each setting it measures: the in-to-out delay after several starts (the spread between
starts is part of the answer), the delay after a short and a long freeze of the engine (does it
come back), and the dropouts during a quiet hold. One JSON line per setting.

    latmatrix.py 1 --engine /usr/local/bin/camilladsp --set 256:512 --set 128:256 --set 64:128
"""
import argparse, json, os, re, signal, subprocess, sys, time
import yaml
import websockets.sync.client as wsc

URL = "ws://127.0.0.1:1234"
HERE = os.path.dirname(os.path.abspath(__file__))
WORK = "/tmp/k1000-latmatrix"
UNIT = "k1000-bench-engine"
ACTIVE = "/var/lib/k1000/active.yml"
DROPOUT = re.compile(r"underrun|overrun|xrun|stalled|PlaybackError|CaptureError", re.IGNORECASE)


def sh(*cmd, check=True):
    return subprocess.run(list(cmd), capture_output=True, text=True, check=check)


def ask(w, name, value=None):
    w.send(json.dumps({"command": name} if value is None else {"command": name, "value": value}))
    r = json.loads(w.recv())
    return r.get("value")


def wait_running(timeout=20.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            with wsc.connect(URL, max_size=None, open_timeout=2) as w:
                if ask(w, "GetState") == "Running":
                    return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def engine_pid():
    out = sh("systemctl", "show", "-p", "MainPID", "--value", UNIT, check=False).stdout.strip()
    return int(out) if out.isdigit() and int(out) > 0 else None


def stop_engine():
    sh("sudo", "systemctl", "stop", UNIT, check=False)
    sh("sudo", "systemctl", "reset-failed", UNIT, check=False)


def prime_spectrum(seconds):
    """Ask for the spectrum from the first moment the websocket answers, the way K1000's own
    spectrum connection does at an engine start. On 5.0.0-beta.1 the FIRST spectrum request makes
    the audio threads allocate their ring buffers; when that lands after playback has started it
    costs one underrun, and the delay stays higher. Asked early, it lands before."""
    t0 = time.monotonic()
    n = 0
    while time.monotonic() - t0 < seconds:
        try:
            with wsc.connect(URL, max_size=None, open_timeout=0.5) as w:
                while time.monotonic() - t0 < seconds:
                    w.send(json.dumps({"command": "GetSpectrum", "value": {
                        "side": "capture", "channel": None, "min_freq": 20.0, "max_freq": 20000.0,
                        "n_bins": 30}}))
                    w.recv()
                    n += 1
                    time.sleep(0.05)
        except Exception:
            time.sleep(0.02)
    return n


def start_engine(binary, cfg, log, prime="early"):
    stop_engine()
    try:
        os.remove(log)
    except OSError:
        pass
    sh("sudo", "systemd-run", "--quiet", "--collect", f"--unit={UNIT}",
       "-p", "User=k1000", "-p", "Group=audio", "-p", "AmbientCapabilities=CAP_SYS_NICE",
       "-p", "LimitRTPRIO=95", "-p", "LimitMEMLOCK=infinity",
       binary, "-v", "-p", "1234", "-a", "127.0.0.1", "-o", log, cfg)
    if prime == "early":
        prime_spectrum(1.5)
    ok = wait_running()
    if ok and prime == "late":
        time.sleep(5.0)
        prime_spectrum(1.5)
    return ok


def log_lines(log):
    try:
        with open(log, errors="replace") as f:
            return f.read().splitlines()
    except OSError:
        return []


def dropouts(log):
    return [l for l in log_lines(log) if DROPOUT.search(l)]


def device_sizes(log):
    """What the engine says it got from the devices (debug lines)."""
    out = {}
    for l in log_lines(log):
        m = re.search(r"(Capture|Playback|PB|Cap)?.*?Device is using a (buffer|period) size of (\d+) frames", l)
        if m:
            out.setdefault(m.group(2), []).append(int(m.group(3)))
    return out


def measure(out_ch, avg=4):
    r = subprocess.run([sys.executable, os.path.join(HERE, "m4loop.py"), str(out_ch), "--avg", str(avg)],
                       capture_output=True, text=True)
    try:
        j = json.loads(r.stdout)
    except ValueError:
        return {"error": (r.stderr or r.stdout)[-300:]}
    res = {"ms": j["latency_ms"], "snr": j["comb_snr"], "level": j["buffer_level_frames"]}
    if j.get("latency_warning"):
        res["warning"] = j["latency_warning"]
    return res


def freeze(ms):
    pid = engine_pid()
    if not pid:
        return False
    os.kill(pid, signal.SIGSTOP)
    try:
        time.sleep(ms / 1000.0)
    finally:
        os.kill(pid, signal.SIGCONT)
    return True


def run_setting(a, binary, chunk, target):
    row = {"engine": os.path.basename(binary), "chunk": chunk, "target": target}
    with open(ACTIVE) as f:
        cfg = yaml.safe_load(f)
    cfg["devices"]["chunksize"] = chunk
    cfg["devices"]["target_level"] = target
    path = f"{WORK}/cfg-{chunk}-{target}.yml"
    with open(path, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)
    chk = sh(binary, "-c", path, check=False)
    if chk.returncode != 0:
        row["error"] = "config refused: " + (chk.stdout + chk.stderr).strip().splitlines()[-1][-200:]
        return row
    log = f"{WORK}/engine-{chunk}-{target}.log"
    starts = []
    for i in range(a.starts):
        if not start_engine(binary, path, log, a.prime):
            row["error"] = "the engine did not reach Running: " + " | ".join(log_lines(log)[-3:])[-400:]
            stop_engine()
            return row
        time.sleep(a.settle)
        at_start = dropouts(log)
        m = measure(a.out)
        m["dropouts_at_start"] = len(at_start)
        m["dropouts_while_measuring"] = len(dropouts(log)) - len(at_start)
        if at_start:
            m["first_dropout"] = at_start[0][11:23] + " " + at_start[0].split("> ", 1)[-1][:70]
        starts.append(m)
    row["starts"] = starts
    row["devices"] = device_sizes(log)
    ok = [s["ms"] for s in starts if "ms" in s and "warning" not in s and not s["dropouts_at_start"]]
    row["clean_starts"] = len(ok)
    if ok:
        row["start_ms_min"], row["start_ms_max"] = min(ok), max(ok)
    # The engine of the last start keeps running for the freezes and the hold.
    gaps = []
    for ms in a.gap:
        before = len(dropouts(log))
        if not freeze(ms):
            gaps.append({"freeze_ms": ms, "error": "no engine process"})
            break
        time.sleep(a.settle)
        if not wait_running(5):
            gaps.append({"freeze_ms": ms, "error": "the engine is not running after the freeze",
                         "log": dropouts(log)[before:][-3:]})
            break
        m = measure(a.out)
        m.update(freeze_ms=ms, new_dropout_lines=len(dropouts(log)) - before)
        gaps.append(m)
    row["gaps"] = gaps
    if a.hold > 0 and engine_pid():
        before = len(dropouts(log))
        with wsc.connect(URL, max_size=None, open_timeout=2) as w:
            ask(w, "GetProcessingLoad")
            time.sleep(a.hold)
            row["hold"] = {"seconds": a.hold, "dropout_lines": len(dropouts(log)) - before,
                           "load": round(float(ask(w, "GetProcessingLoad")), 2),
                           "level": ask(w, "GetBufferLevel")}
        m = measure(a.out)
        row["hold"]["ms_after"] = m.get("ms")
    stop_engine()
    return row


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out", type=int, help="the M4 output looped back (1-4)")
    ap.add_argument("--engine", action="append", required=True, help="engine binary (repeatable)")
    ap.add_argument("--set", action="append", required=True, metavar="CHUNK:TARGET", help="repeatable")
    ap.add_argument("--starts", type=int, default=3, help="starts measured per setting")
    ap.add_argument("--gap", type=float, action="append", default=None,
                    help="freeze lengths in ms (default 15 and 50)")
    ap.add_argument("--hold", type=float, default=45.0, help="quiet seconds watched for dropouts")
    ap.add_argument("--settle", type=float, default=12.0)
    ap.add_argument("--prime", choices=("early", "late"), default="early",
                    help="when the first spectrum request is made: as the engine starts (default, "
                         "like K1000) or 5 s after it runs (shows what a late first request costs)")
    a = ap.parse_args()
    a.gap = a.gap if a.gap is not None else [15.0, 50.0]
    os.makedirs(WORK, exist_ok=True)
    sets = [tuple(int(x) for x in s.split(":")) for s in a.set]
    sh("sudo", "systemctl", "stop", "k1000.service", "camilladsp.service")
    try:
        for binary in a.engine:
            ver = sh(binary, "--version", check=False).stdout.strip()
            print(json.dumps({"engine": binary, "version": ver}), flush=True)
            for chunk, target in sets:
                try:
                    row = run_setting(a, binary, chunk, target)
                except Exception as e:                      # one bad setting must not end the run
                    row = {"engine": os.path.basename(binary), "chunk": chunk, "target": target,
                           "error": f"{type(e).__name__}: {e}"}
                    stop_engine()
                print(json.dumps(row), flush=True)
    finally:
        stop_engine()
        sh("sudo", "systemctl", "start", "camilladsp.service", "k1000.service", check=False)


if __name__ == "__main__":
    main()
