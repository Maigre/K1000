#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# K1000 laptop access (docs/06-phone.md §11): the Friday tune's way in over the K1000 Wi-Fi.
# Run as root by k1000-laptop.service ONLY (started by k1000-laptop.path when the k1000 service
# creates /run/k1000/laptop-open: SETUP > Laptop access, double push).
#
#   k1000-laptop-access.sh run      open, then keep it open while the request stands, 2 h at most
#   k1000-laptop-access.sh close    remove the opening (the unit's ExecStopPost: every way out)
#   k1000-laptop-access.sh status   what is open now (for a human)
#
# What it opens: ONE rule, inserted at the head of the AP fence's input chain (nftables table
# inet k1000_ap, loaded by k1000-ap-fence.service from /etc/k1000/ap-fence.nft):
#   iifname "<AP iface>" ip saddr <AP subnet> ip daddr <AP address> tcp dport { 22, 5005 } ct state new accept
# tagged with the comment "k1000-laptop-access". That is SSH and CamillaGUI, from the K1000
# Wi-Fi's own subnet (10.42.0.0/24) to the box's AP address only, nothing else. Nothing about it
# comes from the request: k1000 can only ask, not choose ports, addresses or duration.
# Never persistent: the rule is in the running ruleset only (the fence file never holds it; a
# reboot or a fence reload starts closed) and the request flag is in /run.
# It closes by itself: after K1000_LAPTOP_S (2 h, counted on the boot clock: a clock set by NTP
# mid-show changes nothing), when the flag goes (the box closed it; k1000 stopped, which removes
# /run/k1000), when the fence goes, and whenever the unit stops (ExecStopPost runs `close`).
# Connections already open stay until they end (the fence keeps established connections); new
# ones are refused at once.
# A fence reloaded while open (setup-ap.sh re-run) loses the rule: it is put back within 10 s.
# Status for the box screen: $STATE_DIR/status (key=value: state open|closed, until/at on the
# boot clock in seconds, reason box|time|k1000|stopped|no-fence|no-ap|error), written before the
# flag is removed, so the box always sees why. Lines go to the journal: journalctl -t k1000-laptop.
# Exit: 0 closed normally; 1 refused (no fence, no AP settings) or failed; 2 bad usage.
# Test hooks (never needed on the box): K1000_LAPTOP_FLAG (/run/k1000/laptop-open),
# K1000_LAPTOP_STATE_DIR (/run/k1000-laptop), K1000_AP_ENV (/etc/k1000/ap.env), K1000_UPTIME
# (/proc/uptime), K1000_LAPTOP_S (7200, at most 7200), K1000_LAPTOP_TICK_S (1),
# K1000_LAPTOP_CHECK_TICKS (10: ticks between two checks of the rule).
set -u
PATH=${PATH:+$PATH:}/usr/sbin:/usr/bin:/sbin:/bin

FLAG=${K1000_LAPTOP_FLAG:-/run/k1000/laptop-open}
STATE_DIR=${K1000_LAPTOP_STATE_DIR:-/run/k1000-laptop}
AP_ENV=${K1000_AP_ENV:-/etc/k1000/ap.env}
UPTIME=${K1000_UPTIME:-/proc/uptime}
DURATION=${K1000_LAPTOP_S:-7200}
TICK=${K1000_LAPTOP_TICK_S:-1}
CHECK_TICKS=${K1000_LAPTOP_CHECK_TICKS:-10}
TAG=k1000-laptop-access
PORTS="22, 5005"

case $DURATION in ''|*[!0-9]*) DURATION=7200 ;; esac
[ "$DURATION" -le 7200 ] || DURATION=7200          # never longer than 2 h, whatever the env says
[ "$DURATION" -ge 1 ] || DURATION=7200
case $CHECK_TICKS in ''|*[!0-9]*|0) CHECK_TICKS=10 ;; esac

say() { echo "$*"; }            # the journal (the unit's SyslogIdentifier=k1000-laptop)

uptime_s() {
  read -r up _ < "$UPTIME" 2>/dev/null || up=0
  echo "${up%%.*}"
}

# write_status KEY=VALUE...: the status file, replaced atomically (the box reads it every second)
write_status() {
  mkdir -p "$STATE_DIR" 2>/dev/null || true
  tmp="$STATE_DIR/.status.$$"
  if { for kv in "$@"; do printf '%s\n' "$kv"; done; } > "$tmp" 2>/dev/null; then
    chmod 0644 "$tmp" && mv -f "$tmp" "$STATE_DIR/status"
  fi
  rm -f "$tmp"
}

set_reason() {
  mkdir -p "$STATE_DIR" 2>/dev/null || true
  printf '%s\n' "$1" > "$STATE_DIR/reason" 2>/dev/null || true
}

fence_loaded() { nft list table inet k1000_ap >/dev/null 2>&1; }

# the AP's interface, address and subnet from setup-ap.sh's ap.env, checked (they go into a rule)
read_ap() {
  [ -r "$AP_ENV" ] || return 1
  IFACE=$(sed -n 's/^K1000_AP_IFACE=//p' "$AP_ENV" | head -n 1)
  CIDR=$(sed -n 's/^K1000_AP_ADDR=//p' "$AP_ENV" | head -n 1)
  case $IFACE in ''|*[!A-Za-z0-9_.-]*) return 1 ;; esac
  [ ${#IFACE} -le 15 ] || return 1
  ADDR=${CIDR%/*}
  [ "${CIDR#*/}" = 24 ] || return 1                  # setup-ap.sh: always a /24
  echo "$ADDR" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}$' || return 1
  SUBNET="${ADDR%.*}.0/24"
  return 0
}

handles() { nft -a list chain inet k1000_ap input 2>/dev/null | sed -n "/comment \"$TAG\"/s/.*# handle \([0-9][0-9]*\).*/\1/p"; }

del_rules() {
  n=0
  for h in $(handles); do
    nft delete rule inet k1000_ap input handle "$h" 2>/dev/null && n=$((n + 1))
  done
  echo "$n"
}

add_rule() {
  nft insert rule inet k1000_ap input iifname "\"$IFACE\"" ip saddr "$SUBNET" ip daddr "$ADDR" \
    tcp dport "{ $PORTS }" ct state new accept comment "\"$TAG\""
}

rule_present() { [ -n "$(handles)" ]; }

do_close() {
  reason=$(cat "$STATE_DIR/reason" 2>/dev/null || true)
  [ -n "$reason" ] || reason=stopped
  removed=0
  if fence_loaded; then removed=$(del_rules); fi
  write_status state=closed "reason=$reason" "at=$(uptime_s)"
  rm -f "$STATE_DIR/reason"
  rm -f "$FLAG"                 # after the status: the box sees why before the flag goes
  if fence_loaded && rule_present; then
    say "ERROR: the opening is still in the fence after closing"
    return 1
  fi
  if [ "$removed" != 0 ] || [ "$reason" != none ]; then
    say "laptop access CLOSED ($reason): CamillaGUI and SSH fenced off the K1000 Wi-Fi again"
  fi
  return 0
}

do_run() {
  rm -f "$STATE_DIR/reason"
  if [ ! -e "$FLAG" ]; then
    set_reason none
    say "no request ($FLAG): nothing opened"
    return 0
  fi
  if ! fence_loaded; then
    set_reason no-fence
    say "REFUSED: the AP fence (nftables table inet k1000_ap) is not loaded: no K1000 Wi-Fi to open"
    return 1
  fi
  if ! read_ap; then
    set_reason no-ap
    say "REFUSED: no usable AP settings in $AP_ENV (K1000_AP_IFACE, K1000_AP_ADDR=a.b.c.d/24)"
    return 1
  fi
  del_rules >/dev/null
  if ! add_rule; then
    set_reason error
    say "ERROR: nft refused the opening rule"
    return 1
  fi
  start=$(uptime_s)
  until=$((start + DURATION))
  write_status state=open "until=$until" "at=$start" "ports=22,5005" "subnet=$SUBNET" "iface=$IFACE"
  say "laptop access OPEN for $((DURATION / 60)) min: tcp 22 (SSH) and 5005 (CamillaGUI) from $SUBNET to $ADDR on $IFACE"
  ticks=0
  while :; do
    if [ ! -e "$FLAG" ]; then
      if [ -d "$(dirname "$FLAG")" ]; then set_reason box; else set_reason k1000; fi
      break
    fi
    now=$(uptime_s)
    if [ "$now" -ge "$until" ]; then
      set_reason time
      break
    fi
    ticks=$((ticks + 1))
    if [ $((ticks % CHECK_TICKS)) = 0 ]; then
      if ! fence_loaded; then
        set_reason no-fence
        say "the AP fence went away: closing"
        break
      fi
      if ! rule_present; then
        if add_rule; then say "the fence was reloaded: opening put back"; else set_reason error; break; fi
      fi
    fi
    sleep "$TICK"
  done
  return 0
}

do_status() {
  if fence_loaded && rule_present; then
    echo "laptop access: OPEN"
    nft list chain inet k1000_ap input 2>/dev/null | grep -F "comment \"$TAG\"" || true
  else
    echo "laptop access: closed"
  fi
  cat "$STATE_DIR/status" 2>/dev/null || true
  return 0
}

case ${1:-} in
  run) do_run; exit $? ;;
  close) do_close; exit $? ;;
  status) do_status; exit $? ;;
  *) echo "usage: $0 run|close|status" >&2; exit 2 ;;
esac
