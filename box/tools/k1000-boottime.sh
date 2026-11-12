#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# K1000 boot timer: where THIS boot's seconds went, counted from power-on (firmware included).
# Read-only: it reads two logs and prints a table; it changes nothing on the box.
#
#   sudo ./k1000-boottime.sh              the table for the current boot
#   sudo ./k1000-boottime.sh --save DIR   the same, and keep the two raw logs in DIR (vclog.txt,
#                                         journal.txt) so a measurement can be re-read later
#
# How it counts. `systemd-analyze` and the journal start at the kernel; the firmware's seconds
# before it (10.4 s on 16/10: bootloader, config.txt, HDMI/camera probing, initramfs, kernel image)
# are only in the VideoCore log, `vclog --msg` (root), stamped in ms since power-on. So:
#   Linux start = the firmware log's "Starting ARM" line (the last one: the log is a ring and prints
#                 out of order, it is sorted first);
#   any journal line = Linux start + (its monotonic stamp - the boot's first kernel line's stamp),
#                 which holds whether the monotonic clock starts at kernel entry or carries an offset.
# No "Starting ARM" line (vclog missing, no root, a firmware that words it differently): the table is
# printed after Linux start instead, and says so.
#
# The events (first match in this boot's journal):
#   userspace start     systemd's first line
#   k1000 started       "k1000 <version>: profile ..."
#   engine Running      "engine Running" (k1000 saw the engine processing audio)
#   ramp start          "boot ramp: start" (older k1000: "pushing the state, ramping in")
#   Main at level       "boot ramp: done" (the saved master reached: full level)
#   first screen frame  "screen: first frame" (older k1000: "screen on /dev/fb...")
#
# K1000_BOOTTIME_VCLOG / K1000_BOOTTIME_JOURNAL: read these files instead of running vclog and
# journalctl (tests, or a log saved with --save).
set -euo pipefail

SAVE=""
while [ $# -gt 0 ]; do
  case $1 in
    --save) [ $# -ge 2 ] || { echo "k1000-boottime: --save needs a directory" >&2; exit 2; }; SAVE=$2; shift ;;
    -h|--help) awk 'NR > 2 && /^#/ { sub(/^# ?/, ""); print; next } NR > 2 { exit }' "$0"; exit 0 ;;
    *) echo "k1000-boottime: unknown option '$1' (try --help)" >&2; exit 2 ;;
  esac
  shift
done

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

if [ -n "${K1000_BOOTTIME_VCLOG:-}" ]; then
  cp "$K1000_BOOTTIME_VCLOG" "$tmp/vclog.txt"
elif command -v vclog >/dev/null; then
  vclog --msg > "$tmp/vclog.txt" 2> "$tmp/vclog.err" || true
else
  echo "vclog not found" > "$tmp/vclog.err"; : > "$tmp/vclog.txt"
fi
if [ -n "${K1000_BOOTTIME_JOURNAL:-}" ]; then
  cp "$K1000_BOOTTIME_JOURNAL" "$tmp/journal.txt"
else
  journalctl -b -o short-monotonic --no-pager > "$tmp/journal.txt" 2>/dev/null || true
fi
if [ -n "$SAVE" ]; then
  mkdir -p "$SAVE"
  cp "$tmp/vclog.txt" "$SAVE/vclog.txt"
  cp "$tmp/journal.txt" "$SAVE/journal.txt"
fi

# Linux start, in seconds after power-on: "%06i.%03i: <msg>" = ms.us since power-on (vclog.c).
fw=$(LC_ALL=C sort -n "$tmp/vclog.txt" | awk '
  /Starting ARM/ { if (match($0, /^[[:space:]]*[0-9]+\.[0-9]+:/)) { t = substr($0, RSTART, RLENGTH - 1) + 0; s = t / 1000 } }
  END { if (s != "") printf "%.6f", s }')

awk -v fw="$fw" '
  # "[   12.345678] host ident[pid]: message"
  match($0, /^\[[[:space:]]*[0-9]+\.[0-9]+\]/) {
    m = substr($0, RSTART + 1, RLENGTH - 2) + 0
    if (first == "") first = m
    msg = substr($0, RLENGTH + 1)
    if (!("user" in at) && msg ~ / systemd\[1\]: /) at["user"] = m
    if (!("k1000" in at) && msg ~ /: INFO k1000: k1000 [^ ]+: profile /) at["k1000"] = m
    if (!("run" in at) && msg ~ /k1000\.control: engine Running$/) at["run"] = m
    if (!("ramp" in at) && msg ~ /k1000\.control: boot ramp: start/) at["ramp"] = m
    if (!("ramp_old" in at) && msg ~ /pushing the state, ramping in/) at["ramp_old"] = m
    if (!("level" in at) && msg ~ /k1000\.control: boot ramp: done/) at["level"] = m
    if (!("frame" in at) && msg ~ /k1000: screen: first frame/) at["frame"] = m
    if (!("frame_old" in at) && msg ~ /k1000: screen on \/dev\/fb/) at["frame_old"] = m
  }
  function row(label, key, alt, altnote,   t, note) {
    note = ""
    if (!(key in at) && alt != "" && (alt in at)) { key = alt; note = "  (" altnote ")" }
    if (!(key in at)) { printf "  %-20s %7s\n", label, "-"; return }
    t = at[key] - first + base
    printf "  %-20s %7.2f%s\n", label, t, note
  }
  END {
    if (first == "") { print "k1000-boottime: no journal lines for this boot (journalctl -b)" > "/dev/stderr"; exit 1 }
    if (fw != "") {
      base = fw + 0
      print "k1000-boottime: this boot, seconds after power-on"
      printf "  %-20s %7.2f\n", "Linux start", base
    } else {
      base = 0
      print "k1000-boottime: this boot, seconds after LINUX START (no firmware \"Starting ARM\" line: run as root, vclog --msg)"
    }
    row("userspace start", "user")
    row("k1000 started", "k1000")
    row("engine Running", "run")
    row("ramp start", "ramp", "ramp_old", "sync start: older k1000")
    row("Main at level", "level")
    row("first screen frame", "frame", "frame_old", "screen opened: older k1000")
  }' "$tmp/journal.txt"
if [ -z "$fw" ] && [ -s "$tmp/vclog.err" ]; then
  echo "  (vclog: $(head -n 1 "$tmp/vclog.err"))"
fi
