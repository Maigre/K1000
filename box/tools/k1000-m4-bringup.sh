#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# k1000-m4-bringup: the MOTU M4 bring-up helper (Tuesday 13). Run it ON THE BOX.
#
#   k1000-m4-bringup.sh
#       CHECKS ONLY. Nothing plays, no service or file is touched:
#       1 card + USB id · 2 USB stream formats/channels/rates (read from /proc, no PCM opened) ·
#       3 hw params (aplay/arecord --dump-hw-params with S16 mono, which a 4-channel hw: device
#       refuses; only when nothing holds the M4) · 4 mixer · 5 /etc/k1000/k1000.yaml against what
#       the M4 offers · 6 power · 7 the front-panel positions to set · 8 what to do next.
#       Exit 0 = no FAIL line, 1 = at least one FAIL.
#
#   sudo k1000-m4-bringup.sh --tone --i-have-turned-the-speakers-off [--freq HZ]
#       Per-output IDENTIFICATION, opt-in: output N plays N short beeps, then again, at -40 dBFS
#       (about -24 dBu at the M4 line outs, ~26 dB under a Meyer limiter), one output at a time;
#       Enter moves to the next. Watch the M4's output meters (or put a TRS loop from the output
#       into front input 1, or headphones on the cable end) and label each cable. --freq: 30-5000
#       Hz (default 1000; a sub's own low-pass hides 1 kHz, the meters do not).
#       Refuses to run without --i-have-turned-the-speakers-off. If the engine holds the M4, it
#       asks you to type STOP before it stops k1000 and camilladsp, and starts them again at the
#       end, also after Ctrl-C or an error (the engine comes back muted, k1000 ramps it in).
#
# Env (tests and odd setups): K1000_SETTINGS (/etc/k1000/k1000.yaml), K1000_ASOUND (/proc/asound),
#   K1000_ACTIVE (/var/lib/k1000/active.yml), K1000_BRINGUP_APLAY (aplay), K1000_BRINGUP_ARECORD
#   (arecord), K1000_BRINGUP_SYSTEMCTL (systemctl), K1000_BRINGUP_TTY (/dev/tty: answers are read
#   from it), K1000_BRINGUP_ALLOW_NONROOT=1 (tests only: --tone without root).
#
# Why the tone is like this: -40 dBFS is fixed (the generator refuses anything louder, and
# truncates samples toward zero so quantisation never rounds a peak above it); beeps start
# and end with 10 ms fades (no click); `hw:` is opened directly with the format the M4 offers, so
# no ALSA plugin converts or mixes anything; the other outputs carry digital zero.
set -euo pipefail

SETTINGS=${K1000_SETTINGS:-/etc/k1000/k1000.yaml}
ASOUND=${K1000_ASOUND:-/proc/asound}
ACTIVE=${K1000_ACTIVE:-/var/lib/k1000/active.yml}
APLAY=${K1000_BRINGUP_APLAY:-aplay}
ARECORD=${K1000_BRINGUP_ARECORD:-arecord}
SYSTEMCTL=${K1000_BRINGUP_SYSTEMCTL:-systemctl}
TTY_IN=${K1000_BRINGUP_TTY:-/dev/tty}
RATE=48000
TONE_DBFS=-40

usage() { sed -n '3,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; }

TONE=0 SPEAKERS_OFF=0 FREQ=1000
while [ $# -gt 0 ]; do
  case $1 in
    --tone) TONE=1 ;;
    --i-have-turned-the-speakers-off) SPEAKERS_OFF=1 ;;
    --freq)
      [ $# -ge 2 ] || { echo "k1000-m4-bringup: --freq needs a value" >&2; exit 2; }
      FREQ=$2; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "k1000-m4-bringup: unknown option '$1' (try --help)" >&2; exit 2 ;;
  esac
  shift
done
if ! [[ $FREQ =~ ^[0-9]+$ ]] || [ "$FREQ" -lt 30 ] || [ "$FREQ" -gt 5000 ]; then
  echo "k1000-m4-bringup: --freq must be a whole number of Hz, 30-5000" >&2; exit 2
fi
if [ $TONE = 1 ] && [ $SPEAKERS_OFF != 1 ]; then
  cat >&2 <<'EOF'
k1000-m4-bringup: REFUSED. The identification tone plays on every M4 output in turn.
Turn the Meyers OFF (or unplug their XLRs) first, then run it again with
  --tone --i-have-turned-the-speakers-off
EOF
  exit 2
fi

TMP=$(mktemp -d)
FACTS=$TMP/facts

# ------------------------------------------------------------------------------------- checks
CHECK_PY=$(cat <<'PY'
import os, re, subprocess, sys
from pathlib import Path

settings_path, asound, active_path, facts_path, aplay, arecord, rate = sys.argv[1:8]
asound, rate = Path(asound), int(rate)
fails, warns = [], []


def say(tag, msg):
    print(f"  {tag:<5} {msg}")
    if tag == "FAIL":
        fails.append(msg)
    elif tag == "WARN":
        warns.append(msg)


def head(title):
    print(f"\n== {title}")


def read(p):
    try:
        return Path(p).read_text(errors="replace")
    except OSError:
        return None


def run(cmd, timeout=6):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (p.stdout or "") + (p.stderr or ""), p.returncode
    except (OSError, subprocess.SubprocessError) as e:
        return f"{cmd[0]}: {e}", -1


# The same rules as k1000/profiles.py (card_id, output_map aliases), repeated so this tool works
# even if the package does not import.
CARD = re.compile(r"^(?:plug)?hw:(?:CARD=)?([^,]+)")
LOGICAL = ("Sub L", "Sub R", "Top L", "Top R")
ALIASES = {"subl": 0, "sub_l": 0, "sub l": 0, "subr": 1, "sub_r": 1, "sub r": 1,
           "topl": 2, "top_l": 2, "top l": 2, "topr": 3, "top_r": 3, "top r": 3}
CDSP_TO_ALSA = {"S16_LE": "S16_LE", "S24_3_LE": "S24_3LE", "S24_4_LE": "S24_LE", "S32_LE": "S32_LE",
                "F32_LE": "FLOAT_LE", "F64_LE": "FLOAT64_LE"}
PREFERENCE = ("S32_LE", "S24_3LE", "S24_LE", "S16_LE", "FLOAT_LE", "FLOAT64_LE")
DEFAULT_CONTROLS = {"encoders": [0, 1, 2, 3], "pushes": [0, 1, 2, 3], "buttons": [4, 5, 6, 7],
                    "reverse": [False, False, False, False]}


def card_id(dev):
    m = CARD.match(str(dev).strip())
    if not m:
        return None
    cid = m.group(1)
    return f"card{cid}" if cid.isdigit() else cid


def logical(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if 0 <= v <= 3 else None
    if isinstance(v, str):
        return ALIASES.get(v.strip().lower())
    return None


def parse_stream(text):
    out, sec = {}, None
    for raw in text.splitlines():
        line = raw.strip()
        if line in ("Playback:", "Capture:"):
            sec = line[:-1].lower()
            out.setdefault(sec, {"fmts": [], "chans": [], "rates": [], "status": ""})
            continue
        if sec is None:
            continue
        key, sep, val = line.partition(":")
        if not sep:
            continue
        acc = out[sec]
        if key == "Status":
            acc["status"] = val.strip()
        elif key == "Format":
            acc["fmts"] += [f for f in val.split() if f not in acc["fmts"]]
        elif key == "Channels":
            acc["chans"] += [int(n) for n in re.findall(r"\d+", val)]
        elif key == "Rates":
            nums = [int(n) for n in re.findall(r"\d+", val)]
            if "continuous" in val and len(nums) >= 2:
                acc["rates"].append((min(nums), max(nums)))
            else:
                acc["rates"] += [(n, n) for n in nums]
    return out


def rate_ok(d):
    rates = d.get("rates") or []
    return any(lo <= rate <= hi for lo, hi in rates) if rates else None


def rates_text(d):
    return ", ".join(str(lo) if lo == hi else f"{lo}-{hi}" for lo, hi in d.get("rates") or []) or "?"


# --- settings ----------------------------------------------------------------------------
cfg, settings_err = {}, None
try:
    import yaml
    raw = read(settings_path)
    if raw is None:
        settings_err = f"cannot read {settings_path}"
    else:
        cfg = yaml.safe_load(raw) or {}
        if not isinstance(cfg, dict):
            cfg, settings_err = {}, f"{settings_path} is not a mapping"
except Exception as e:      # yaml missing or broken file: the built-in m4 defaults still apply
    cfg, settings_err = {}, f"{settings_path}: {e}"
profiles = cfg.get("profiles") if isinstance(cfg.get("profiles"), dict) else {}
m4 = profiles.get("m4") if isinstance(profiles.get("m4"), dict) else {}
prof = {"capture_device": "hw:M4", "playback_device": "hw:M4", "capture_channels": None,
        "playback_channels": None, "capture_format": None, "playback_format": None,
        "input_map": [2, 3], "output_map": ["TopL", "TopR", "SubL", "SubR"]}
prof.update({k: v for k, v in m4.items() if k in prof})
cid = card_id(prof["playback_device"])

# --- 1. card -----------------------------------------------------------------------------
head("1. Card")
cards = read(asound / "cards")
print("  /proc/asound/cards:")
for line in (cards or "(unreadable)\n").rstrip().splitlines():
    print(f"    {line}")
motu = []
for d in sorted(asound.glob("card[0-9]*")):
    usb = (read(d / "usbid") or "").strip()
    if usb.startswith("07fd:"):
        motu.append(((read(d / "id") or d.name).strip(), d.name, usb))
present = bool(cid) and (asound / cid).exists()
usbid = (read(asound / cid / "usbid") or "").strip() if present else ""
if not cid:
    say("FAIL", f"playback_device {prof['playback_device']!r} in {settings_path} is not an hw: name")
elif present:
    rev = {"07fd:0008": "1st hardware revision (the kernel waits ~4 s at plug-in)",
           "07fd:000b": "2nd hardware revision"}.get(usbid, "not a known M4 id")
    say("PASS", f"card {cid} registered ({(asound / cid).resolve().name}), USB {usbid or '?'}: {rev}")
    if usbid and not usbid.startswith("07fd:"):
        say("WARN", f"{cid} is not a MOTU device (USB {usbid})")
else:
    say("FAIL", f"card {cid} is not registered: M4 unplugged, switched off, or under another name")
for mid, mcard, musb in motu:
    if mid != cid:
        say("FAIL", f"a MOTU device is {mcard} with id {mid!r} (USB {musb}): set capture_device and "
                    f"playback_device to hw:{mid} in {settings_path} (profiles: m4:)")
if prof["capture_device"] != prof["playback_device"]:
    say("WARN", f"capture_device {prof['capture_device']} differs from playback_device "
                f"{prof['playback_device']}: v0 runs one card (one clock)")
lsusb, _rc = run(["lsusb", "-d", "07fd:"])
if lsusb.strip():
    print("  lsusb: " + lsusb.strip().replace("\n", "\n         "))
dm, dm_rc = run(["dmesg"])
if dm_rc == 0:
    hits = [l for l in dm.splitlines() if re.search(r"07fd|MOTU|\bM4\b", l)]
    if hits:
        print("  dmesg (last MOTU lines):")
        for l in hits[-6:]:
            print(f"    {l}")
else:
    print("  (dmesg needs sudo: run with sudo to see the kernel's USB lines)")

# --- 2. stream ---------------------------------------------------------------------------
head("2. USB stream (/proc, no PCM opened)")
stream = parse_stream(read(asound / cid / "stream0") or "") if present else {}
busy = False
for sec, pcm in (("playback", "pcm0p"), ("capture", "pcm0c")):
    d = stream.get(sec)
    status = (read(asound / cid / pcm / "sub0" / "status") or "").strip() if present else ""
    hwp = (read(asound / cid / pcm / "sub0" / "hw_params") or "").strip() if present else ""
    if status and status != "closed":
        busy = True
    if not d or not d["chans"]:
        if present:
            say("FAIL", f"{sec}: no stream description in {asound / cid / 'stream0'}")
        continue
    print(f"  {sec:<8}: channels {'/'.join(str(c) for c in sorted(set(d['chans'])))}, "
          f"formats {' '.join(d['fmts']) or '?'}, rates {rates_text(d)}")
    print(f"            open now: {'no' if status in ('', 'closed') else 'YES (' + status.splitlines()[0] + ')'}"
          + ("" if hwp in ("", "closed") else "; " + ", ".join(
              l.strip() for l in hwp.splitlines() if l.split(":")[0] in ("format", "channels", "rate"))))
if busy:
    print("  The M4 is open (the engine holds it): section 3 is skipped, the stream above is the reference.")

# --- 3. hw params ------------------------------------------------------------------------
head("3. hw params (aplay/arecord --dump-hw-params, S16 mono: refused by a 4-channel device)")
dump = {}
if not present:
    print("  skipped: no card")
elif busy:
    print("  skipped: the M4 is in use")
else:
    for tool, sec, sink in ((aplay, "playback", "/dev/zero"), (arecord, "capture", "/dev/null")):
        text, _rc = run([tool, "-D", f"hw:{cid}", "--dump-hw-params", "-f", "S16_LE", "-c", "1",
                         "-r", str(rate), "-d", "1", sink])
        vals = {}
        for line in text.splitlines():
            key, sep, val = line.partition(":")
            if sep and key.strip() in ("FORMAT", "CHANNELS", "RATE"):
                vals[key.strip()] = val.strip()
        last = [l for l in text.strip().splitlines() if l.strip()][-1:] or ["(no output)"]
        print(f"  {sec:<8}: " + ("  ".join(f"{k} {v}" for k, v in vals.items()) or "no HW params table")
              + f"   [{last[0].strip()}]")
        dump[sec] = vals
        chans = [int(n) for n in re.findall(r"\d+", vals.get("CHANNELS", ""))]
        d = stream.get(sec)
        if chans and d and d["chans"] and max(chans) != max(d["chans"]):
            say("WARN", f"{sec}: hw params say CHANNELS {vals['CHANNELS']}, stream0 says {max(d['chans'])}")

# --- 4. mixer ----------------------------------------------------------------------------
head("4. Mixer (amixer -c <card> contents)")
if present:
    text, rc = run(["amixer", "-c", cid, "contents"])
    lines = text.strip().splitlines()
    for l in lines[:40]:
        print(f"    {l}")
    if len(lines) > 40:
        print(f"    ... ({len(lines) - 40} more lines)")
    print("  Any playback volume must read 0 dB and any switch 'on'; after fixing one: sudo alsactl store")
else:
    print("  skipped: no card")

# --- 5. settings vs the device -----------------------------------------------------------
head(f"5. {settings_path} (profiles: m4) against the M4")
if settings_err:
    say("WARN", f"{settings_err}: checking the built-in m4 defaults")
pin = cfg.get("profile")
if pin == "m4":
    say("PASS", "profile: m4 (pinned, the gig rule)")
else:
    say("INFO", f"profile: {pin!r}: switch to m4 once this report has no FAIL (section 8)")
pb, cp = stream.get("playback") or {}, stream.get("capture") or {}
counts = {}
for sec, d in (("playback", pb), ("capture", cp)):
    want = prof[f"{sec}_channels"]
    have = max(d["chans"]) if d.get("chans") else None
    if isinstance(want, bool) or (want is not None and not isinstance(want, int)):
        say("FAIL", f"{sec}_channels {want!r} is not a number (null = probe)")
        continue
    if have is None:
        counts[sec] = want or 4
        if present:
            say("WARN", f"{sec} channels unknown from /proc; k1000 assumes {want or 4}")
        continue
    if want is None:
        counts[sec] = have
        say("PASS", f"{sec}_channels: null (probe) -> {have}")
    elif want in d["chans"]:
        counts[sec] = want
        say("PASS", f"{sec}_channels: {want} (offered)")
    else:
        counts[sec] = have
        say("FAIL", f"{sec}_channels: {want} is not offered (device: {sorted(set(d['chans']))}); "
                    "set it to null so k1000 probes it")
    fmt = prof[f"{sec}_format"]
    if fmt is None:
        best = next((f for f in PREFERENCE if f in d.get("fmts", [])), None)
        if best:
            say("PASS", f"{sec}_format: null -> CamillaDSP picks {best}")
        else:
            say("FAIL", f"{sec}: no format CamillaDSP can use in {d.get('fmts')}")
    elif CDSP_TO_ALSA.get(fmt) in d.get("fmts", []):
        say("PASS", f"{sec}_format: {fmt} (offered)")
    else:
        say("FAIL", f"{sec}_format: {fmt} is not offered ({d.get('fmts')}); set it to null")
    ok = rate_ok(d)
    if ok is True:
        say("PASS", f"{sec}: {rate} Hz offered")
    elif ok is False:
        say("FAIL", f"{sec}: {rate} Hz NOT offered ({rates_text(d)})")
imap = prof["input_map"]
ncap = counts.get("capture") or 4
if (isinstance(imap, list) and len(imap) == 2 and all(isinstance(i, int) and not isinstance(i, bool)
                                                       for i in imap)):
    if all(0 <= i < ncap for i in imap):
        hint = " = rear LINE IN 3/4 (the program inputs)" if imap == [2, 3] else ""
        say("PASS", f"input_map {imap}: program L/R from capture channels {imap[0] + 1}/{imap[1] + 1}{hint}")
    else:
        say("FAIL", f"input_map {imap}: the M4 has capture channels 0-{ncap - 1}")
else:
    say("FAIL", f"input_map {imap!r}: needs two capture channel numbers (0-based)")
omap = prof["output_map"]
npb = counts.get("playback") or 4
labels = []
if isinstance(omap, list):
    idx = [logical(v) for v in omap]
    labels = [LOGICAL[i] if i is not None else "?" for i in idx]
    table = ", ".join(f"out{n + 1} {lab}" for n, lab in enumerate(labels))
    if None in idx:
        say("FAIL", f"output_map {omap!r}: use 0-3 or SubL/SubR/TopL/TopR")
    elif len(idx) != npb or sorted(set(idx)) != [0, 1, 2, 3]:
        say("FAIL", f"output_map {omap!r} ({table}): needs {npb} entries covering Sub L, Sub R, Top L, Top R")
    else:
        say("PASS", f"output_map: {table}")
else:
    say("FAIL", f"output_map {omap!r} is not a list")
ctl = cfg.get("controls") if isinstance(cfg.get("controls"), dict) else {}
if all(ctl.get(k, v) == v for k, v in DEFAULT_CONTROLS.items()):
    say("WARN", "controls: still the defaults (encoders [0,1,2,3], buttons [4,5,6,7]): UNCONFIRMED "
                "unless SETUP > Knob test proved them. Hold-B2 = MUTE depends on it (README: Knobs and buttons)")
else:
    say("INFO", f"controls: {ctl} (set by hand)")
act = read(active_path)
if act is not None:
    title = re.search(r"^title:\s*(.*)$", act, re.M)
    devs = re.findall(r"^\s+(capture|playback):\s*\n((?:\s{4,}.*\n?)+)", act, re.M)
    print(f"  engine config now ({active_path}): {title.group(1).strip() if title else '?'}")
    for sec, block in devs:
        kv = dict(re.findall(r"^\s+(type|channels|device|format):\s*(\S+)", block, re.M))
        print(f"    {sec:<8}: " + ", ".join(f"{k} {v}" for k, v in kv.items()))

# --- 6. power ----------------------------------------------------------------------------
head("6. Power (the M4 is bus-powered from the Pi)")
thr, rc = run(["vcgencmd", "get_throttled"])
val = thr.strip().partition("=")[2]
if rc != 0 or not val:
    say("WARN", f"vcgencmd get_throttled: {thr.strip() or 'no output'}")
elif val == "0x0":
    say("PASS", "throttled=0x0 (check again after 10 minutes of program)")
else:
    say("FAIL", f"throttled={val}: under-voltage or heat (bit 0 = under-voltage now, bit 16 = since boot)")

# --- facts for --tone --------------------------------------------------------------------
fmt_tone = next((f for f in ("S32_LE", "S24_3LE", "S16_LE") if f in pb.get("fmts", [])), "")
safe = lambda s: re.sub(r"[^A-Za-z0-9 ._:-]", "", str(s))
with open(facts_path, "w") as f:
    f.write(f"present={1 if present else 0}\ncard={safe(cid or '')}\nbusy={1 if busy else 0}\n"
            f"channels={max(pb['chans']) if pb.get('chans') else 0}\nformat={fmt_tone}\n"
            f"rate_ok={1 if rate_ok(pb) else 0}\nlabels={'|'.join(safe(l) for l in labels)}\n")
print(f"\nRESULT: {len(fails)} FAIL, {len(warns)} WARN")
sys.exit(1 if fails else 0)
PY
)

PANEL=$(cat <<'EOF'

== 7. M4 front panel: set it BEFORE any program, then tape it (research, see notes)
  MONITOR knob ............ fully clockwise (outs 1-2 = tops at unity; outs 3-4 have no knob)
                            UNVERIFIED that fully clockwise is exactly unity: loop test (README)
  INPUT MONITOR MIX ....... fully clockwise (USB playback only, no direct input monitoring)
                            UNVERIFIED that it mutes the inputs completely: leak test (README)
  MON 1 / MON 2 / 3-4 ..... all UNLIT. Lit = the inputs go straight to outs 1-2, past K1000 and
                            past its MUTE. Tape over the buttons.
  48V ..................... off      GAIN 1 / GAIN 2 ... fully counter-clockwise
  Phones .................. down     Rear power switch . on, taped
  Program ................. rear LINE IN 3 (L) and 4 (R), balanced TRS
  USB ..................... short USB-C to USB-A cable, Pi black (USB 2) port, strain-relieved
  K1000 knobs ............. Master (E1, SHOW page) at -20 dB or lower before any program
EOF
)

NEXT=$(cat <<'EOF'

== 8. Next (by hand; this tool changes nothing unless --tone)
  a. Outputs, speakers OFF:   sudo /opt/k1000/tools/k1000-m4-bringup.sh --tone --i-have-turned-the-speakers-off
  b. Pin the M4 (a device change: ~1 s of silence, bench only): set `profile: m4` in
     /etc/k1000/k1000.yaml, then `sudo systemctl restart k1000`. Expect in `journalctl -u k1000 -n 40`
     "profile m4 (settings)" and no "profile m4:" warning; in /var/lib/k1000/active.yml capture and
     playback type Alsa, device hw:M4, channels 4; no ERROR/underrun in
     /var/log/k1000/camilladsp_rCURRENT.log; no NO DEVICE on the screen. Run this tool again:
     section 2 then shows what the engine opened.
  c. Presence drills, speakers off: pull the USB -> NO DEVICE within ~1 s and
     `journalctl -u camilladsp` says "waiting for audio device(s) hw:M4"; plug it back -> the
     engine starts within ~1 s of the card (rev 1: +4 s) and k1000 ramps Main in.
  d. 1-hour soak with program: sudo systemd-run --unit=k1000-soak --uid=k1000 /opt/k1000/tools/k1000-soak.sh
     then /opt/k1000/tools/k1000-soak.sh --summary
EOF
)

set +e
python3 -I -c "$CHECK_PY" "$SETTINGS" "$ASOUND" "$ACTIVE" "$FACTS" "$APLAY" "$ARECORD" "$RATE"
CHECK_RC=$?
set -e
if [ $TONE = 0 ]; then
  printf '%s\n%s\n' "$PANEL" "$NEXT"
  rm -rf "$TMP"
  exit $CHECK_RC
fi

# ------------------------------------------------------------------------------------- --tone
declare -A F=()
while IFS='=' read -r k v; do F[$k]=$v; done < "$FACTS"
rm -rf "$TMP"
CARD=${F[card]:-} CH=${F[channels]:-0} FMT=${F[format]:-}
IFS='|' read -r -a LABELS <<< "${F[labels]:-}"

echo
echo "== Identification tone"
[ "${F[present]:-0}" = 1 ] || { echo "REFUSED: no M4 card (section 1)." >&2; exit 1; }
[ -n "$FMT" ] && [ "$CH" -ge 1 ] || { echo "REFUSED: no playable format/channel count for hw:$CARD (section 2)." >&2; exit 1; }
[ "${F[rate_ok]:-0}" = 1 ] || { echo "REFUSED: hw:$CARD does not offer $RATE Hz (section 2)." >&2; exit 1; }
if [ "$(id -u)" != 0 ] && [ "${K1000_BRINGUP_ALLOW_NONROOT:-}" != 1 ]; then
  echo "REFUSED: run it with sudo (it may have to stop and start the K1000 services)." >&2; exit 2
fi
if ! exec 4<"$TTY_IN"; then
  echo "REFUSED: needs a terminal for the questions ($TTY_IN)." >&2; exit 2
fi

TONE_PY=$(cat <<'PY'
import sys
import numpy as np
ch, idx, fmt, rate, freq, beeps, dbfs = (int(sys.argv[1]), int(sys.argv[2]), sys.argv[3],
                                         int(sys.argv[4]), float(sys.argv[5]), int(sys.argv[6]),
                                         float(sys.argv[7]))
if not (dbfs <= -40.0 and 0 <= idx < ch and 1 <= beeps <= 8 and 30 <= freq <= 5000):
    sys.exit("tone: refused (level above -40 dBFS or bad arguments)")
amp = 10.0 ** (dbfs / 20.0)


def silence(s):
    return np.zeros(int(round(s * rate)))


n_on, n_fade = int(0.25 * rate), int(0.01 * rate)
env = np.ones(n_on)
ramp = 0.5 - 0.5 * np.cos(np.pi * np.arange(n_fade) / n_fade)
env[:n_fade], env[-n_fade:] = ramp, ramp[::-1]
beep = amp * env * np.sin(2 * np.pi * freq * np.arange(n_on) / rate)
parts = [silence(0.3)]
for _rep in range(2):
    for _b in range(beeps):
        parts += [beep, silence(0.25)]
    parts.append(silence(0.75))
frames = np.zeros((sum(p.size for p in parts), ch))
frames[:, idx] = np.concatenate(parts)
if fmt == "S32_LE":
    data = np.trunc(frames * 2147483647.0).astype("<i4").tobytes()
elif fmt == "S24_3LE":
    data = np.trunc(frames * 8388607.0).astype("<i4").reshape(-1).view(np.uint8).reshape(-1, 4)[:, :3].tobytes()
elif fmt == "S16_LE":
    data = np.trunc(frames * 32767.0).astype("<i2").tobytes()
else:
    sys.exit(f"tone: format {fmt} not supported")
sys.stdout.buffer.write(data)
PY
)

STOPPED=()
# shellcheck disable=SC2329  # invoked by the EXIT trap below
restore() {
  local rc=$? j u
  trap - EXIT INT TERM HUP
  for ((j = ${#STOPPED[@]} - 1; j >= 0; j--)); do      # camilladsp first (boots muted), then k1000
    u=${STOPPED[j]}
    if "$SYSTEMCTL" start "$u.service"; then
      echo "started $u.service"
    else
      echo "ERROR: could not start $u.service: run 'sudo systemctl start $u'" >&2; rc=1
    fi
  done
  exit "$rc"
}

if [ "${F[busy]:-0}" = 1 ]; then
  cat <<EOF
The engine holds hw:$CARD. To play the tone, k1000 and camilladsp must stop: the K1000 audio
stops until they are started again at the end (the engine comes back muted, k1000 ramps it in).
EOF
  printf 'Type STOP to stop them now (anything else cancels): '
  answer=
  read -r answer <&4 || true
  if [ "$answer" != STOP ]; then
    echo "cancelled: nothing was stopped, nothing played."; exit 1
  fi
  trap restore EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM HUP
  for u in k1000 camilladsp; do
    case $("$SYSTEMCTL" is-active "$u.service" 2>/dev/null || true) in
      active|activating|reloading)
        STOPPED+=("$u")
        "$SYSTEMCTL" stop "$u.service"
        echo "stopped $u.service" ;;
    esac
  done
  for _ in $(seq 50); do
    st=$(cat "$ASOUND/$CARD/pcm0p/sub0/status" 2>/dev/null || echo closed)
    [ "$st" = closed ] && break
    sleep 0.1
  done
  if [ "$(cat "$ASOUND/$CARD/pcm0p/sub0/status" 2>/dev/null || echo closed)" != closed ]; then
    echo "hw:$CARD is still open by something else: giving up (services are started again)." >&2; exit 1
  fi
fi

echo "Speakers OFF. Each output N plays N beeps twice, $TONE_DBFS dBFS, $FREQ Hz, $FMT, $CH channels."
for ((i = 0; i < CH; i++)); do
  n=$((i + 1)) label=${LABELS[i]:-}
  while :; do
    echo "  out $n${label:+ ($label)}: $n beep(s)"
    if ! python3 -I -c "$TONE_PY" "$CH" "$i" "$FMT" "$RATE" "$FREQ" "$n" "$TONE_DBFS" \
         | "$APLAY" -q -D "hw:$CARD" -t raw -f "$FMT" -c "$CH" -r "$RATE"; then
      echo "ERROR: playing on hw:$CARD failed (busy? unplugged?)" >&2; exit 1
    fi
    printf '  [Enter] next output   [r] repeat   [q] stop: '
    a=q
    read -r a <&4 || a=q
    case $a in
      r|R) continue ;;
      q|Q) echo "stopped at out $n."; break 2 ;;
      *) break ;;
    esac
  done
done
printf 'Identification done. Label the cables:'
for ((i = 0; i < CH; i++)); do printf ' out%d = %s.' $((i + 1)) "${LABELS[i]:-?}"; done
echo
exit 0
