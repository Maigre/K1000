#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# K1000 AP subnet guard: a NetworkManager dispatcher script, installed by box/net/setup-ap.sh as
# /etc/NetworkManager/dispatcher.d/90-k1000-ap-guard (docs/06-phone.md §1, §7). NetworkManager runs
# it as root on every interface event: $1 = the interface, $2 = the action.
#
# 1. The AP never shares its subnet with another interface. A laptop on a direct eth0 cable with
#    NetworkManager's "Shared to other computers" is 10.42.0.1/24 and hands the box 10.42.0.x:
#    with the AP up, the laptop's own address is local to the box (on wlan0) and SSH over eth0 is
#    lost. When such an address appears (the cable plugged in after boot, a lease renewed), the AP
#    is stopped; once it is gone, the AP this guard stopped (or the hostapd unit refused) is
#    started again, unless it was taken down meanwhile (setup-ap.sh --down: ap.info state=down).
#    The hostapd unit refuses to start while the clash lasts (its ExecStartPre), so a boot with
#    the cable already in ends the same way.
# 2. --backend nm: NetworkManager's AP never stays up without its fence (nftables table
#    inet k1000_ap); the hostapd unit checks that itself.
# It only ever stops or starts the AP, never the interface that triggered it. Its lines go to the
# journal (journalctl -t k1000-ap-guard).
# Test hooks: K1000_AP_ENV (/etc/k1000/ap.env), K1000_AP_INFO (/etc/k1000/ap.info),
# K1000_AP_GUARD_MARK (/run/k1000-ap-guard.stopped).
set -u
PATH=${PATH:+$PATH:}/usr/sbin:/usr/bin:/sbin:/bin
ENV_FILE=${K1000_AP_ENV:-/etc/k1000/ap.env}
INFO_FILE=${K1000_AP_INFO:-/etc/k1000/ap.info}
MARK=${K1000_AP_GUARD_MARK:-/run/k1000-ap-guard.stopped}
UNIT=k1000-ap-hostapd.service
CON=k1000-ap

iface=${1:-} action=${2:-}
case $action in up|down|dhcp4-change|reapply|connectivity-change) ;; *) exit 0 ;; esac
[ -r "$ENV_FILE" ] || exit 0                       # no AP set up on this box
AP_IFACE=$(sed -n 's/^K1000_AP_IFACE=//p' "$ENV_FILE" | head -n 1)
AP_ADDR=$(sed -n 's/^K1000_AP_ADDR=//p' "$ENV_FILE" | head -n 1)
[ -n "$AP_IFACE" ] && [ -n "$AP_ADDR" ] || exit 0

say() {
  logger -t k1000-ap-guard -- "$*" 2>/dev/null || true
  echo "k1000-ap-guard: $*"
}
hostapd_up() { systemctl is-active --quiet "$UNIT"; }
nm_up() { nmcli -t -f NAME connection show --active 2>/dev/null | grep -qxF "$CON"; }

# 2. NetworkManager's AP came up without its fence
if [ "$iface" = "$AP_IFACE" ] && [ "$action" = up ] && nm_up && ! nft list table inet k1000_ap >/dev/null 2>&1; then
  nmcli connection down "$CON" >/dev/null 2>&1 || true
  say "the $CON connection taken down: the fence (table inet k1000_ap) is not loaded"
fi

# 1. the subnet
clash=$(ip -4 -o addr show to "$AP_ADDR" 2>/dev/null | while read -r _ dev _ addr _; do
  [ "$dev" = "$AP_IFACE" ] || printf '%s holds %s, ' "$dev" "$addr"
done)
if [ -n "$clash" ]; then
  stopped=""
  if hostapd_up; then systemctl stop --no-block "$UNIT" && stopped=$UNIT; fi
  if nm_up; then nmcli connection down "$CON" >/dev/null 2>&1 && stopped="${stopped:+$stopped and }the $CON connection"; fi
  if [ -n "$stopped" ]; then
    : > "$MARK"
    say "AP stopped ($stopped): ${clash%, }, inside the AP's subnet $AP_ADDR (a laptop on eth0 sharing its connection?). SSH over eth0 keeps working; the AP comes back once that address is gone"
  fi
  exit 0
fi
[ -e "$MARK" ] || exit 0
rm -f "$MARK"
if grep -qx 'state=down' "$INFO_FILE" 2>/dev/null; then exit 0; fi   # the no-go came meanwhile
if systemctl is-enabled --quiet "$UNIT" 2>/dev/null; then
  systemctl reset-failed "$UNIT" k1000-ap-dnsmasq.service >/dev/null 2>&1 || true
  systemctl start --no-block "$UNIT"
  say "the AP's subnet is free again: $UNIT started"
elif nmcli -t -f NAME connection show 2>/dev/null | grep -qxF "$CON"; then
  say "the AP's subnet is free again: bring the $CON connection back with sudo /opt/k1000/net/setup-ap.sh"
fi
exit 0
