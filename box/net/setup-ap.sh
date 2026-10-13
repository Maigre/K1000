#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# K1000 Wi-Fi access point (D-007, docs/06-phone.md): the box's own Wi-Fi for the phone page.
# Idempotent, run as root. A re-run with nothing new changes nothing and says "0 changes".
# Every choice below sticks (it is kept in /etc/k1000/ap.info) until it is given again.
#
#   sudo ./setup-ap.sh                create/refresh the AP and bring it up (first time: hostapd,
#                                     PMF off, 5 GHz channel 36, country FR, captive portal off,
#                                     SSH fenced off)
#   sudo ./setup-ap.sh --band bg      2.4 GHz, channel 6 instead (OUTDOOR gigs: in the EU, 5 GHz
#                                     channel 36 is indoor-only; also for phones without 5 GHz,
#                                     and usually the longer range through the box's metal case)
#   sudo ./setup-ap.sh --band a       back to 5 GHz, channel 36
#   sudo ./setup-ap.sh --channel N    another channel in the chosen band (5 GHz: 36, 40, 44, 48, the
#                                     non-DFS ones; 2.4 GHz: 1-13)
#   sudo ./setup-ap.sh --portal on    captive portal: every name -> the box, so Android raises "Sign
#                                     in to network" (the D-007 bonus). Costs the automatic rejoin
#                                     after a dropout (docs/06-phone.md §5)
#   sudo ./setup-ap.sh --portal off   back to the default: only k1000.home.arpa resolves, so Android
#                                     treats K1000 as "Wi-Fi without internet" and, once told "Stay
#                                     connected / Don't ask again", keeps auto-joining it
#   sudo ./setup-ap.sh --ssh on       also allow SSH (22) from the K1000 Wi-Fi (default: off)
#   sudo ./setup-ap.sh --backend nm   NetworkManager's own AP (wpa_supplicant) instead of hostapd.
#                                     NOT recommended on the Pi's onboard chip: it drops many phones
#                                     ~6 s after they join (verified on this chip: docs/06-phone.md §6)
#   sudo ./setup-ap.sh --backend hostapd   back to hostapd + its own dnsmasq (the default; needs the
#                                     hostapd package, which install.sh installs)
#   sudo ./setup-ap.sh --iface wlan1  the AP on another Wi-Fi interface (a USB dongle + antenna)
#   sudo ./setup-ap.sh --new-password a new Wi-Fi password (every phone must scan the QR again)
#   sudo ./setup-ap.sh --no-up        configure only (it comes up at the next boot)
#   sudo ./setup-ap.sh --status       the AP, its clients, the fence, country, radio, subnet clashes
#   sudo ./setup-ap.sh --down         the Friday no-go: take the AP down and KEEP it down, across
#                                     reboots and across later plain runs (install.sh --ap,
#                                     bootstrap.sh); the fence stays. With `phone: enabled: false`
#   sudo ./setup-ap.sh --up           bring the AP back after --down (a plain run keeps it down)
#   sudo ./setup-ap.sh --remove       delete the AP, its DNS file, units, guard and fence
#                                     (keeps /etc/k1000/ap.psk, so a re-setup keeps the password)
# Exit: 0 done; 1 error (nothing or part done, said why); 2 bad option; 4 the AP is not up: refused
# (another interface in its subnet, or the interface is a Wi-Fi client) or it did not stay up.
#
# What it sets up:
#   - The AP on wlan0 only: SSID K1000, WPA2-PSK (CCMP), PMF OFF (the Pi's Broadcom AP drops
#     clients when PMF is advertised: docs/06-phone.md §6), band a / ch 36
#     by default, 10.42.0.1/24, DHCP + DNS for the phones, no IPv6, on at boot.
#     Backend hostapd (the default; same source: NetworkManager's wpa_supplicant AP on this
#     chip completes the handshake, then drops the client before DHCP): k1000-ap-hostapd.service
#     (ieee80211w left unset) + k1000-ap-dnsmasq.service, wlan0 unmanaged by NetworkManager
#     (/etc/NetworkManager/conf.d/k1000-ap-unmanaged.conf), /etc/k1000/{hostapd.conf,ap.env,
#     ap-dnsmasq.conf}. A k1000-ap connection left by the nm backend is kept, down and not
#     autoconnecting.
#     Backend nm: the NetworkManager connection "k1000-ap" (ipv4.method shared, NM's own dnsmasq,
#     pmf 1 = disabled, power save off).
#   - /etc/k1000/ap.psk: the password, generated once (12 easy-to-type characters, no 0/o/1/l/i),
#     root:k1000 0640 so the k1000 service can show it as a QR on the box screen. It is shown on
#     the box screen only.
#   - /etc/NetworkManager/dnsmasq-shared.d/k1000.conf: the AP's DNS, portal on or off (above).
#   - The fence, k1000-ap-fence.service + /etc/k1000/ap-fence.nft (its own nftables table, loaded
#     at boot before any network): from the AP, only DHCP, DNS, 80, 443, 8080 (+22 with --ssh on)
#     reach the box, at most 16 connections per phone and 64 in all to the page's ports; nothing
#     on the AP is routed anywhere; the AP address (10.42.0.1) is unreachable from eth0. Without
#     it every phone on K1000 would reach CamillaGUI (:5005, no authentication). The hostapd unit
#     requires it and refuses to start without its table.
#   - The subnet guard. A laptop on a direct eth0 cable with NetworkManager's "Shared to other
#     computers" is 10.42.0.1/24 too and hands the box 10.42.0.x: with the AP up, the laptop's own
#     address would be local to the box (wlan0) and SSH over eth0 would be lost. So the AP never
#     comes up while another interface holds an address in 10.42.0.0/24: this script refuses
#     (exit 4), the hostapd unit refuses at every start, and the NetworkManager dispatcher script
#     /etc/NetworkManager/dispatcher.d/90-k1000-ap-guard (k1000-ap-guard.sh) stops the AP when such
#     an address appears later (the cable plugged in after boot) and starts it again once it is gone.
#   - Not on a Wi-Fi client link: when the interface is connected to another Wi-Fi (a card set up
#     with Wi-Fi in Raspberry Pi Imager), it refuses (exit 4) instead of cutting that link.
#   - The hostapd unit is bound to its interface (drop-in k1000-ap-hostapd.service.d/
#     k1000-iface.conf: BindsTo= the interface's device): it starts once the interface exists and
#     stops if it goes (a USB dongle unplugged), with no restart loop.
#   - /etc/k1000/ap.info (0644): what is set (ssid, band, channel, iface, backend, portal, ssh, and
#     state up/down), so the box screen shows what is really there and a later run keeps it.
#   - Wi-Fi country FR (raspi-config), Wi-Fi radio unblocked, and the packages this needs marked
#     "manually installed" so an `apt autoremove` never takes them (D-006 purges desktop packages).
# What it never touches: eth0 or any other NetworkManager connection, the k1000/camilladsp
# services, NetworkManager's own nftables tables, nftables.service.
# Env: K1000_AP_IFACE (default wlan0), K1000_AP_SSID (default K1000), K1000_AP_COUNTRY (FR).
# Test hooks (never needed on the box): K1000_AP_ETC (/etc/k1000), K1000_AP_DNS_DST,
# K1000_AP_SYS_NET (/sys/class/net), K1000_AP_UNIT_DIR (/etc/systemd/system),
# K1000_AP_NM_CONF_DIR (/etc/NetworkManager/conf.d), K1000_AP_RFKILL (/sys/class/rfkill),
# K1000_AP_DISPATCH_DIR (/etc/NetworkManager/dispatcher.d), K1000_AP_GUARD_MARK
# (/run/k1000-ap-guard.stopped), K1000_AP_SETTLE_S (2: seconds between the two hostapd starts of a
# handover), K1000_AP_CHECK_S (3: seconds hostapd must stay up after a start).
set -euo pipefail

SRC=$(cd "$(dirname "$0")" && pwd -P)
CON=k1000-ap
SSID=${K1000_AP_SSID:-K1000}
COUNTRY=${K1000_AP_COUNTRY:-FR}
ADDR=10.42.0.1/24
IP=${ADDR%/*}
SUBNET=10.42.0.0/24
ETC=${K1000_AP_ETC:-/etc/k1000}
PSK_FILE=$ETC/ap.psk
INFO_FILE=$ETC/ap.info
FENCE_FILE=$ETC/ap-fence.nft
HOSTAPD_CONF=$ETC/hostapd.conf
AP_ENV=$ETC/ap.env
APDNS_CONF=$ETC/ap-dnsmasq.conf
DNS_DST=${K1000_AP_DNS_DST:-/etc/NetworkManager/dnsmasq-shared.d/k1000.conf}
SYS_NET=${K1000_AP_SYS_NET:-/sys/class/net}
SYS_RFKILL=${K1000_AP_RFKILL:-/sys/class/rfkill}
UNIT_DIR=${K1000_AP_UNIT_DIR:-/etc/systemd/system}
NM_CONF_DIR=${K1000_AP_NM_CONF_DIR:-/etc/NetworkManager/conf.d}
UNMANAGED_CONF=$NM_CONF_DIR/k1000-ap-unmanaged.conf
DISPATCH_DIR=${K1000_AP_DISPATCH_DIR:-/etc/NetworkManager/dispatcher.d}
GUARD=$DISPATCH_DIR/90-k1000-ap-guard
GUARD_MARK=${K1000_AP_GUARD_MARK:-/run/k1000-ap-guard.stopped}
SETTLE_S=${K1000_AP_SETTLE_S:-2}
CHECK_S=${K1000_AP_CHECK_S:-3}
FENCE_UNIT=k1000-ap-fence.service
HOSTAPD_UNIT=k1000-ap-hostapd.service
APDNS_UNIT=k1000-ap-dnsmasq.service
DROPIN=$UNIT_DIR/$HOSTAPD_UNIT.d/k1000-iface.conf
HOSTAPD_RUN=/run/hostapd
PSK_ALPHABET=abcdefghjkmnpqrstuvwxyz23456789
KEEP_PKGS=(network-manager dnsmasq-base wpasupplicant iw rfkill nftables hostapd)
NEED_PKGS=(network-manager dnsmasq-base wpasupplicant nftables)

BAND="" CHANNEL="" MODE=setup NEW_PSK=0 DO_UP=1 PORTAL="" SSH="" BACKEND="" IFACE_ARG="" WANT_UP=0
while [ $# -gt 0 ]; do
  case $1 in
    --band) BAND=${2:-}; shift ;;
    --band=*) BAND=${1#*=} ;;
    --channel) CHANNEL=${2:-}; shift ;;
    --channel=*) CHANNEL=${1#*=} ;;
    --portal) PORTAL=${2:-}; shift ;;
    --portal=*) PORTAL=${1#*=} ;;
    --ssh) SSH=${2:-}; shift ;;
    --ssh=*) SSH=${1#*=} ;;
    --backend) BACKEND=${2:-}; shift ;;
    --backend=*) BACKEND=${1#*=} ;;
    --iface) IFACE_ARG=${2:-}; shift ;;
    --iface=*) IFACE_ARG=${1#*=} ;;
    --new-password) NEW_PSK=1 ;;
    --no-up) DO_UP=0 ;;
    --up) WANT_UP=1 ;;
    --status) MODE=status ;;
    --down) MODE=down ;;
    --remove) MODE=remove ;;
    -h|--help) awk 'NR > 2 && /^#/ { sub(/^# ?/, ""); print; next } NR > 2 { exit }' "$0"; exit 0 ;;
    *) echo "setup-ap.sh: unknown option '$1' (try --help)" >&2; exit 2 ;;
  esac
  shift
done
bad() { echo "setup-ap.sh: $*" >&2; exit 2; }
case $BAND in ""|a|bg) ;; *) bad "--band must be a (5 GHz) or bg (2.4 GHz)" ;; esac
case $PORTAL in ""|on|off) ;; *) bad "--portal must be on or off" ;; esac
case $SSH in ""|on|off) ;; *) bad "--ssh must be on or off" ;; esac
case $BACKEND in ""|nm|hostapd) ;; *) bad "--backend must be nm or hostapd" ;; esac
[[ -z $CHANNEL || $CHANNEL =~ ^[0-9]{1,3}$ ]] || bad "--channel must be a number"
[[ -z $IFACE_ARG || $IFACE_ARG =~ ^[A-Za-z0-9_.-]{1,15}$ ]] || bad "--iface must be an interface name"
[ $WANT_UP = 0 ] || [ $MODE = setup ] || bad "--up goes with a setup run, not with --$MODE"
[ $WANT_UP = 0 ] || [ $DO_UP = 1 ] || bad "--up and --no-up contradict each other"
CHANNEL_ARG=$CHANNEL

CHANGES=0
note() { printf 'k1000-ap: %s\n' "$*"; }
changed() { CHANGES=$((CHANGES + 1)); note "$*"; }
die() { printf 'k1000-ap: ERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root: sudo $0"
command -v nmcli >/dev/null || die "nmcli not found (NetworkManager is required)"
systemctl is-active --quiet NetworkManager || die "NetworkManager is not running"

# What an earlier run set (ap.info), so a plain re-run (e.g. install.sh --ap) keeps it.
prev() { [ -r "$INFO_FILE" ] && sed -n "s/^$1=//p" "$INFO_FILE" | head -n 1 || true; }
IFACE=${IFACE_ARG:-${K1000_AP_IFACE:-$(prev iface)}}
IFACE=${IFACE:-wlan0}
[[ $IFACE =~ ^[A-Za-z0-9_.-]{1,15}$ ]] || IFACE=wlan0
PREV_IFACE=$(prev iface)                  # where the AP was until now (--iface moves it)
BACKEND=${BACKEND:-$(prev backend)}
case $BACKEND in nm|hostapd) ;; *) BACKEND="hostapd" ;; esac   # D-007 as refined: hostapd, PMF off
PORTAL=${PORTAL:-$(prev portal)}
case $PORTAL in on|off) ;; *) PORTAL=off ;; esac   # off: Android keeps auto-joining K1000 (§5)
SSH=${SSH:-$(prev ssh)}
case $SSH in on|off) ;; *) SSH=off ;; esac
# up, or down after --down (the no-go): a plain run keeps a down AP down; only --up lifts it
STATE=$(prev state)
case $STATE in up|down) ;; *) STATE=up ;; esac
[ $WANT_UP = 0 ] || STATE=up
LEASES_NM=/var/lib/NetworkManager/dnsmasq-$IFACE.leases
LEASES_HOSTAPD=/run/k1000-ap/dnsmasq.leases

con_exists() { nmcli -t -f NAME connection show | grep -qxF "$CON"; }
con_active() { nmcli -t -f NAME connection show --active | grep -qxF "$CON"; }
con_file() { nmcli -t -f NAME,FILENAME connection show | awk -F: -v c="$CON" '$1 == c { sub(/^[^:]*:/, ""); print; exit }'; }
unit_active() { systemctl is-active --quiet "$1"; }
unit_enabled() { systemctl is-enabled --quiet "$1" 2>/dev/null; }
fence_loaded() { nft list table inet k1000_ap >/dev/null 2>&1; }
# Addresses in the AP's subnet on any other interface, "eth0 10.42.0.23/24" per line (the
# subnet guard, above). Same test as the hostapd unit's and the dispatcher guard's. The interface
# the AP is leaving (--iface) holds the old AP's own address: not a clash.
subnet_clash() {
  { ip -4 -o addr show to "$SUBNET" 2>/dev/null || true; } | while read -r _ dev _ addr _; do
    [ "$dev" = "$IFACE" ] || [ "$dev" = "${PREV_IFACE:-$IFACE}" ] || printf '%s %s\n' "$dev" "$addr"
  done
}
# --iface moved the AP: the interface it left keeps no AP address (it would clash with the new one)
release_prev_iface() {
  [ -n "$PREV_IFACE" ] && [ "$PREV_IFACE" != "$IFACE" ] || return 0
  if { ip -4 -o addr show dev "$PREV_IFACE" 2>/dev/null || true; } | grep -q " $IP/"; then
    ip addr del "$ADDR" dev "$PREV_IFACE" 2>/dev/null || true
    changed "removed $IP from $PREV_IFACE (the AP moved to $IFACE)"
  fi
}
clash_text() { printf '%s' "$1" | awk 'NF { printf "%s%s holds %s", (n++ ? ", " : ""), $1, $2 }'; }
# The Wi-Fi network $IFACE is a CLIENT of right now (not our AP), or "".
client_link() {
  { nmcli -t -f DEVICE,STATE,CONNECTION device 2>/dev/null || true; } \
    | awk -F: -v i="$IFACE" -v c="$CON" '$1 == i && $2 ~ /^connect/ && $3 != "" && $3 != c { print $3; exit }'
}
# ap.info's state= line (created if absent), every other line kept.
set_info_state() {
  [ "$(prev state)" != "$1" ] || return 0
  install -d -m 0755 "$(dirname "$INFO_FILE")"
  { if [ -r "$INFO_FILE" ]; then grep -v '^state=' "$INFO_FILE" || true; fi; printf 'state=%s\n' "$1"; } > "$INFO_FILE.tmp"
  chmod 0644 "$INFO_FILE.tmp"
  mv -f "$INFO_FILE.tmp" "$INFO_FILE"
  changed "$INFO_FILE: state=$1"
}

# Put $2 (a file) at $1 with mode $3 if it differs; returns 0 when it changed.
put() {
  local dst=$1 src=$2 mode=$3
  if [ -f "$dst" ] && cmp -s "$src" "$dst"; then return 1; fi
  install -D -m "$mode" -o root -g root "$src" "$dst"
  return 0
}
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# --------------------------------------------------------------------------------------- status
if [ $MODE = status ]; then
  note "backend $BACKEND, interface $IFACE, portal $PORTAL, SSH from the AP $SSH, state $STATE$( [ "$STATE" = down ] && echo ' (no-go: --up brings it back)' || true)"
  clash=$(subnet_clash)
  if [ -n "$clash" ]; then
    note "SUBNET CLASH: $(clash_text "$clash"), inside the AP's $SUBNET: the AP is refused/stopped while it lasts (a laptop on eth0 'Shared to other computers'? share on another subnet, e.g. 10.43.0.1/24)"
  else
    note "subnet: no other interface in $SUBNET"
  fi
  [ ! -e "$GUARD_MARK" ] || note "the AP was stopped by the subnet guard ($GUARD_MARK); it comes back once the subnet is free"
  note "guard: $([ -x "$GUARD" ] && echo "$GUARD" || echo 'NOT installed')"
  if con_exists; then
    note "connection $CON: $(con_active && echo ACTIVE || echo inactive)," \
         "autoconnect $(nmcli -g connection.autoconnect connection show "$CON")," \
         "SSID $(nmcli -g 802-11-wireless.ssid connection show "$CON")," \
         "band $(nmcli -g 802-11-wireless.band connection show "$CON")" \
         "channel $(nmcli -g 802-11-wireless.channel connection show "$CON")"
  else
    note "connection $CON: not configured"
  fi
  if [ "$BACKEND" = hostapd ] || [ -f "$UNIT_DIR/$HOSTAPD_UNIT" ]; then
    note "$HOSTAPD_UNIT: $(systemctl is-active "$HOSTAPD_UNIT" 2>/dev/null || true)," \
         "$APDNS_UNIT: $(systemctl is-active "$APDNS_UNIT" 2>/dev/null || true)"
  fi
  note "$IFACE: $(ip -br -4 addr show dev "$IFACE" 2>/dev/null | awk '{print $2, $3}')"
  note "fence: $(fence_loaded && echo 'loaded (table inet k1000_ap)' || echo 'NOT loaded')," \
       "$FENCE_UNIT $(systemctl is-enabled "$FENCE_UNIT" 2>/dev/null || echo 'not installed')"
  note "Wi-Fi radio: $(nmcli radio wifi); country: $(iw reg get 2>/dev/null | awk '/^country/ {sub(/:$/, "", $2); print $2; exit}')"
  for LEASES in "$LEASES_NM" "$LEASES_HOSTAPD"; do
    [ -r "$LEASES" ] || continue
    note "DHCP leases ($LEASES): $(grep -c . "$LEASES" || true)"
    awk '{ printf "k1000-ap:   %s  %s  %s\n", $3, $2, $4 }' "$LEASES"
  done
  note "phones seen (neighbours on $IFACE): $(awk -v i="$IFACE" 'NR > 1 && $6 == i && $3 == "0x2"' /proc/net/arp 2>/dev/null | wc -l)"
  note "DNS: $([ -f "$DNS_DST" ] && echo "$DNS_DST" || echo 'NOT installed')"
  note "password file: $([ -s "$PSK_FILE" ] && stat -c '%n %U:%G %a' "$PSK_FILE" || echo 'missing')"
  exit 0
fi

hostapd_off() {
  local u
  for u in "$APDNS_UNIT" "$HOSTAPD_UNIT"; do
    [ -f "$UNIT_DIR/$u" ] || continue
    if unit_enabled "$u"; then systemctl disable "$u" >/dev/null 2>&1 || true; changed "disabled $u"; fi
    if unit_active "$u"; then systemctl stop "$u" || true; changed "stopped $u"; fi
  done
}

# --------------------------------------------------------------------------------------- down
if [ $MODE = down ]; then
  if con_exists; then
    if [ "$(nmcli -g connection.autoconnect connection show "$CON")" != no ]; then
      nmcli connection modify "$CON" connection.autoconnect no
      changed "autoconnect off: the AP stays down across reboots (undo: sudo $0)"
    fi
    if con_active; then
      nmcli connection down "$CON" >/dev/null
      changed "AP down"
    fi
  fi
  hostapd_off
  rm -f "$GUARD_MARK"                     # the subnet guard never brings it back either
  set_info_state down
  note "the fence stays loaded (harmless without the AP)"
  note "the AP stays down across reboots and later plain runs (install.sh --ap, bootstrap.sh); back: sudo $0 --up"
  note "$CHANGES changes"
  exit 0
fi

# --------------------------------------------------------------------------------------- remove
if [ $MODE = remove ]; then
  if con_exists; then
    nmcli connection delete "$CON" >/dev/null
    changed "deleted the $CON connection"
  fi
  hostapd_off
  RELOAD=0
  for u in "$HOSTAPD_UNIT" "$APDNS_UNIT"; do
    if [ -e "$UNIT_DIR/$u" ]; then rm -f "$UNIT_DIR/$u"; changed "removed $UNIT_DIR/$u"; RELOAD=1; fi
  done
  if [ -e "$DROPIN" ]; then
    rm -f "$DROPIN"; rmdir "$(dirname "$DROPIN")" 2>/dev/null || true
    changed "removed $DROPIN"; RELOAD=1
  fi
  if [ -e "$GUARD" ]; then rm -f "$GUARD"; changed "removed the subnet guard $GUARD"; fi
  rm -f "$GUARD_MARK"
  if [ -e "$UNIT_DIR/$FENCE_UNIT" ]; then
    systemctl disable "$FENCE_UNIT" >/dev/null 2>&1 || true
    unit_active "$FENCE_UNIT" && systemctl stop "$FENCE_UNIT" || true
    rm -f "$UNIT_DIR/$FENCE_UNIT"
    changed "removed the fence ($FENCE_UNIT)"
    RELOAD=1
  fi
  if fence_loaded; then nft delete table inet k1000_ap; changed "unloaded the fence table"; fi
  [ $RELOAD = 0 ] || systemctl daemon-reload
  if [ -e "$UNMANAGED_CONF" ]; then
    rm -f "$UNMANAGED_CONF"
    nmcli general reload conf >/dev/null 2>&1 || systemctl reload NetworkManager || true
    nmcli device set "$IFACE" managed yes >/dev/null 2>&1 || true
    changed "$IFACE is managed by NetworkManager again"
  fi
  for f in "$DNS_DST" "$INFO_FILE" "$FENCE_FILE" "$HOSTAPD_CONF" "$AP_ENV" "$APDNS_CONF"; do
    if [ -e "$f" ]; then rm -f "$f"; changed "removed $f"; fi
  done
  [ -e "$PSK_FILE" ] && note "kept $PSK_FILE (delete it by hand for a new password next time)"
  note "$CHANGES changes"
  exit 0
fi

# --------------------------------------------------------------------------------------- setup
[ -e "$SYS_NET/$IFACE/wireless" ] || [ -e "$SYS_NET/$IFACE/phy80211" ] \
  || die "$IFACE is not a Wi-Fi interface on this box"
# Band/channel: the command line, else what was set (ap.info, then the NM connection), else 5 GHz
# channel 36. An outdoor 2.4 GHz setup survives a later plain re-run (e.g. install.sh --ap).
if [ -z "$BAND" ]; then
  BAND=$(prev band)
  [ -n "$CHANNEL" ] || CHANNEL=$(prev channel)
  if [ -z "$BAND" ] && con_exists; then
    BAND=$(nmcli -g 802-11-wireless.band connection show "$CON" 2>/dev/null || true)
    [ -n "$CHANNEL" ] || CHANNEL=$(nmcli -g 802-11-wireless.channel connection show "$CON" 2>/dev/null || true)
  fi
  case $BAND in a|bg) ;; *) BAND="" CHANNEL=$CHANNEL_ARG ;; esac
fi
BAND=${BAND:-a}
if ! [[ $CHANNEL =~ ^[0-9]{1,3}$ ]] || [ "$CHANNEL" = 0 ]; then
  if [ "$BAND" = a ]; then CHANNEL=36; else CHANNEL=6; fi
fi
# The channel must belong to the band (hostapd would start, then die on it): 5 GHz the non-DFS
# 36-48 (the Pi's chip does no DFS as an AP), 2.4 GHz 1-13 (EU).
if [ "$BAND" = a ]; then ok_channels=" 36 40 44 48 "; else ok_channels=" 1 2 3 4 5 6 7 8 9 10 11 12 13 "; fi
if [[ $ok_channels != *" $CHANNEL "* ]]; then
  [ -z "$CHANNEL_ARG" ] || bad "--channel $CHANNEL is not a band-$BAND channel here (5 GHz: 36, 40, 44, 48; 2.4 GHz: 1-13)"
  if [ "$BAND" = a ]; then CHANNEL=36; else CHANNEL=6; fi
  note "the channel kept from the last run does not fit band $BAND: channel $CHANNEL"
fi

# Before anything changes: never cut a Wi-Fi client link, never the AP in an occupied subnet.
client=$(client_link)
if [ -n "$client" ]; then
  note "ERROR: $IFACE is connected to the Wi-Fi network '$client' as a client: making it the AP would cut"
  note "that link (and an SSH session over it). Nothing changed. Connect over eth0 and remove that Wi-Fi"
  note "connection first, or put the AP on another interface (--iface). Raspberry Pi Imager: no Wi-Fi."
  exit 4
fi
clash=$(subnet_clash)
if [ -n "$clash" ]; then
  if [ $DO_UP = 1 ] && [ "$STATE" = up ]; then
    note "ERROR: $(clash_text "$clash"), inside the AP's subnet $SUBNET. A laptop on eth0 with"
    note "'Shared to other computers' (NetworkManager's 10.42.0.1/24)? With the AP up the box would take the"
    note "laptop's address and SSH over eth0 would be lost: the AP is NOT brought up, nothing changed."
    note "Share on another subnet on the laptop (e.g. 10.43.0.1/24) or use the house network, then run this again."
    exit 4
  fi
  note "WARNING: $(clash_text "$clash"), inside the AP's subnet $SUBNET: the AP will not come up while it lasts"
fi
if [ "$PORTAL" = on ]; then DNS_SRC=$SRC/dnsmasq-k1000.conf; else DNS_SRC=$SRC/dnsmasq-k1000-noportal.conf; fi
for f in "$DNS_SRC" "$SRC/$FENCE_UNIT" "$SRC/$HOSTAPD_UNIT" "$SRC/$APDNS_UNIT" "$SRC/k1000-ap-guard.sh"; do
  [ -f "$f" ] || die "$f not found (run this from box/net/)"
done

# 1. packages: present, and never auto-removed
for p in "${NEED_PKGS[@]}"; do
  dpkg -s "$p" >/dev/null 2>&1 || die "package $p is missing: sudo apt install $p"
done
if [ "$BACKEND" = hostapd ] && { ! dpkg -s hostapd >/dev/null 2>&1 || ! command -v hostapd >/dev/null; }; then
  die "the hostapd backend (the default) needs hostapd: sudo apt install hostapd (while eth0 has internet), or --backend nm"
fi
command -v nft >/dev/null || die "nft not found (sudo apt install nftables): the AP is never up without its fence"
manual=$(apt-mark showmanual 2>/dev/null || true)
for p in "${KEEP_PKGS[@]}"; do
  if dpkg -s "$p" >/dev/null 2>&1 && ! grep -qxF "$p" <<<"$manual"; then
    apt-mark manual "$p" >/dev/null
    changed "apt-mark manual $p (an autoremove will keep it)"
  fi
done

# 2. Wi-Fi country (the regulatory domain allows channel 36) and the radio
if command -v raspi-config >/dev/null; then
  current_country=$(raspi-config nonint get_wifi_country 2>/dev/null || true)
  if [ "$current_country" != "$COUNTRY" ]; then
    raspi-config nonint do_wifi_country "$COUNTRY"
    changed "Wi-Fi country $COUNTRY (raspi-config: cmdline.txt cfg80211.ieee80211_regdom)"
  fi
else
  current_country=$(iw reg get 2>/dev/null | awk '/^country/ {sub(/:$/, "", $2); print $2; exit}')
  if [ "$current_country" != "$COUNTRY" ]; then
    iw reg set "$COUNTRY"
    changed "Wi-Fi country $COUNTRY (iw reg set; not persistent without raspi-config)"
  fi
fi
if [ "$(nmcli radio wifi)" != enabled ]; then
  nmcli radio wifi on
  changed "Wi-Fi radio on (nmcli radio wifi on)"
fi
for r in "$SYS_RFKILL"/rfkill*; do         # no rfkill binary on the box: sysfs does the same
  [ -e "$r/soft" ] && [ "$(cat "$r/type" 2>/dev/null)" = wlan ] || continue
  if [ "$(cat "$r/soft")" = 1 ]; then
    echo 0 > "$r/soft"
    changed "Wi-Fi unblocked ($r)"
  fi
done

# 3. the password: generated once, readable by the k1000 service (it draws the QR)
psk_ok() { [[ $1 =~ ^[[:print:]]{8,63}$ ]]; }
if getent group k1000 >/dev/null; then want_owner="root:k1000" want_mode=640; else want_owner="root:root" want_mode=600; fi
psk=""
[ -s "$PSK_FILE" ] && psk=$(head -n 1 "$PSK_FILE" | tr -d '\r\n')
if [ $NEW_PSK = 1 ] || ! psk_ok "$psk"; then
  raw=""
  while [ ${#raw} -lt 12 ]; do
    raw+=$(head -c 256 /dev/urandom | LC_ALL=C tr -dc "$PSK_ALPHABET")
  done
  psk="${raw:0:4}-${raw:4:4}-${raw:8:4}"
  install -d -m 0755 "$(dirname "$PSK_FILE")"
  # never readable by others, not even for a moment: created 0600, owner and mode set, then moved
  ( umask 077; printf '%s\n' "$psk" > "$PSK_FILE.tmp" )
  chown "$want_owner" "$PSK_FILE.tmp"
  chmod "$want_mode" "$PSK_FILE.tmp"
  mv -f "$PSK_FILE.tmp" "$PSK_FILE"
  changed "new Wi-Fi password in $PSK_FILE (phones must scan the QR on the box again)"
fi
if [ "$(stat -c '%U:%G %a' "$PSK_FILE")" != "$want_owner $want_mode" ]; then
  chown "$want_owner" "$PSK_FILE"
  chmod "$want_mode" "$PSK_FILE"
  changed "$PSK_FILE is $want_owner $want_mode"
fi
[ "$want_owner" = root:k1000 ] || note "WARNING: no k1000 group: the box screen cannot show the Wi-Fi QR"

# 4. the fence: before the AP is up, and checked before it is loaded
RESTART_AP=0
SSH_PORT=""
[ "$SSH" = on ] && SSH_PORT="22, "
cat > "$TMP/fence.nft" <<EOF
# K1000 AP fence: generated by box/net/setup-ap.sh (do not edit: re-run it). Loaded at boot by
# k1000-ap-fence.service. From the K1000 Wi-Fi ($IFACE) only DHCP, DNS, the captive portal (80),
# the phone page (443) and its plain fallback (8080)$( [ "$SSH" = on ] && echo ', and SSH (22)' || true ) reach
# the box, at most 16 open connections per phone and 64 in all to the page's ports (the k1000
# process's file descriptors are the knobs' too); nothing on the AP is routed anywhere; the AP
# address $IP is unreachable from the other interfaces (the box's own connections arrive on lo).
# Only that address: an eth0 network that happens to be $SUBNET as well keeps working.
# Its own table: NetworkManager's are not touched.
table inet k1000_ap
delete table inet k1000_ap
table inet k1000_ap {
	set phone_conns {
		type ipv4_addr
		size 256
		flags dynamic
	}
	chain input {
		type filter hook input priority filter - 10; policy accept;
		iifname "$IFACE" ct state established,related accept
		iifname "$IFACE" udp dport { 53, 67 } accept
		iifname "$IFACE" tcp dport { 80, 443, 8080 } ct state new add @phone_conns { ip saddr ct count over 16 } counter reject with tcp reset
		iifname "$IFACE" tcp dport { 80, 443, 8080 } ct state new ct count over 64 counter reject with tcp reset
		iifname "$IFACE" tcp dport { ${SSH_PORT}53, 80, 443, 8080 } accept
		iifname "$IFACE" icmp type echo-request accept
		iifname "$IFACE" counter drop
		iifname != { "$IFACE", "lo" } ip daddr $IP counter drop
	}
	chain forward {
		type filter hook forward priority filter - 10; policy accept;
		iifname "$IFACE" counter drop
		oifname "$IFACE" counter drop
	}
}
EOF
nft -c -f "$TMP/fence.nft" >/dev/null || die "the fence does not pass 'nft -c' (nothing changed in the firewall)"
FENCE_NEW=0
if put "$FENCE_FILE" "$TMP/fence.nft" 0644; then changed "wrote $FENCE_FILE"; FENCE_NEW=1; fi
UNITS_NEW=0
if put "$UNIT_DIR/$FENCE_UNIT" "$SRC/$FENCE_UNIT" 0644; then changed "installed $UNIT_DIR/$FENCE_UNIT"; UNITS_NEW=1; fi
[ $UNITS_NEW = 0 ] || systemctl daemon-reload
if ! unit_enabled "$FENCE_UNIT"; then
  systemctl enable "$FENCE_UNIT" >/dev/null 2>&1
  changed "enabled $FENCE_UNIT (the fence loads at every boot)"
fi
if ! unit_active "$FENCE_UNIT"; then
  systemctl start "$FENCE_UNIT" || die "$FENCE_UNIT failed: journalctl -u $FENCE_UNIT"
  changed "fence loaded"
elif [ $FENCE_NEW = 1 ] || [ $UNITS_NEW = 1 ] || ! fence_loaded; then
  # reload = nft -f again: the file replaces the table in one transaction (never unfenced)
  systemctl reload "$FENCE_UNIT" || die "$FENCE_UNIT failed: journalctl -u $FENCE_UNIT"
  changed "fence reloaded"
fi
# The subnet guard (NetworkManager runs it on every interface event; both backends).
if put "$GUARD" "$SRC/k1000-ap-guard.sh" 0755; then changed "installed the subnet guard $GUARD"; fi

# 5. DNS for the AP's dnsmasq (NetworkManager's shared mode reads this directory; the hostapd
#    backend's dnsmasq includes the same file)
if put "$DNS_DST" "$DNS_SRC" 0644; then
  changed "installed $DNS_DST (portal $PORTAL)"
  RESTART_AP=1
fi
if command -v dnsmasq >/dev/null && ! dnsmasq --test --conf-file="$DNS_DST" >/dev/null 2>&1; then
  die "$DNS_DST does not pass 'dnsmasq --test'"
fi

# 6. the access point itself
nm_props() {
  printf '%s\n' \
    connection.interface-name "$IFACE" \
    connection.autoconnect "$1" \
    connection.autoconnect-priority 10 \
    802-11-wireless.mode ap \
    802-11-wireless.ssid "$SSID" \
    802-11-wireless.band "$BAND" \
    802-11-wireless.channel "$CHANNEL" \
    802-11-wireless.powersave 2 \
    802-11-wireless-security.key-mgmt wpa-psk \
    802-11-wireless-security.proto rsn \
    802-11-wireless-security.pairwise ccmp \
    802-11-wireless-security.group ccmp \
    802-11-wireless-security.pmf 1 \
    802-11-wireless-security.psk "$psk" \
    ipv4.method shared \
    ipv4.addresses "$ADDR" \
    ipv6.method disabled
}
nm_connection() {       # $1 = autoconnect yes|no; sets CON_CHANGED
  local -a props
  mapfile -t props < <(nm_props "$1")
  CON_CHANGED=0
  if con_exists; then
    local f before after
    f=$(con_file)
    before=$( [ -n "$f" ] && sha256sum "$f" 2>/dev/null | cut -d' ' -f1 || true)
    nmcli connection modify "$CON" "${props[@]}"
    f=$(con_file)
    after=$( [ -n "$f" ] && sha256sum "$f" 2>/dev/null | cut -d' ' -f1 || true)
    if [ "$before" != "$after" ] || [ -z "$after" ]; then
      changed "updated the $CON connection ($SSID, band $BAND, channel $CHANNEL, autoconnect $1)"
      CON_CHANGED=1
    fi
  else
    nmcli connection add type wifi con-name "$CON" "${props[@]}" >/dev/null
    changed "created the $CON connection ($SSID, band $BAND, channel $CHANNEL, $ADDR, autoconnect $1)"
    CON_CHANGED=1
  fi
}

# hostapd really up after a start: still active CHECK_S later, not restarted meanwhile (a channel
# or band the chip refuses makes it die at once, and the unit restarts it), and hostapd_cli says
# ENABLED. Prints why not.
hostapd_check() {
  local n0 n st=""
  n0=$(systemctl show -p NRestarts --value "$HOSTAPD_UNIT" 2>/dev/null || true)
  sleep "$CHECK_S"
  n=$(systemctl show -p NRestarts --value "$HOSTAPD_UNIT" 2>/dev/null || true)
  if ! unit_active "$HOSTAPD_UNIT"; then
    echo "$HOSTAPD_UNIT is $(systemctl is-active "$HOSTAPD_UNIT" 2>/dev/null || true) ${CHECK_S} s after its start"
    return 1
  fi
  if [ "${n:-0}" != "${n0:-0}" ]; then echo "hostapd died and was restarted (NRestarts ${n0:-0} -> $n)"; return 1; fi
  command -v hostapd_cli >/dev/null || return 0
  for _ in 1 2 3 4 5; do
    st=$({ hostapd_cli -p "$HOSTAPD_RUN" -i "$IFACE" status 2>/dev/null || true; } | sed -n 's/^state=//p' | head -n 1)
    [ "$st" != ENABLED ] || return 0
    sleep 1
  done
  echo "hostapd says '${st:-no answer}', not ENABLED"
  return 1
}
band_hint() {
  [ "$BAND" != a ] || note "hint: if the Wi-Fi chip refuses 5 GHz AP mode here, try: sudo $0 --band bg"
}

# ap.env: interface, address and backend, for the hostapd unit and the subnet guard
printf 'K1000_AP_IFACE=%s\nK1000_AP_ADDR=%s\nK1000_AP_BACKEND=%s\n' "$IFACE" "$ADDR" "$BACKEND" > "$TMP/ap.env"
AP_ENV_NEW=0
if put "$AP_ENV" "$TMP/ap.env" 0644; then changed "wrote $AP_ENV"; AP_ENV_NEW=1; fi

HANDOVER=0
if [ "$BACKEND" = nm ]; then
  # back from hostapd: units off, the interface managed by NetworkManager again
  hostapd_off
  if [ -e "$UNMANAGED_CONF" ]; then
    rm -f "$UNMANAGED_CONF"
    nmcli general reload conf >/dev/null 2>&1 || systemctl reload NetworkManager || true
    nmcli device set "$IFACE" managed yes >/dev/null 2>&1 || true
    changed "$IFACE is managed by NetworkManager again"
    RESTART_AP=1
  fi
  release_prev_iface
  if [ "$STATE" = up ]; then nm_connection yes; else nm_connection no; fi
  [ "$CON_CHANGED" = 0 ] || RESTART_AP=1
  if [ "$STATE" = down ] && con_active; then nmcli connection down "$CON" >/dev/null; changed "$CON down (kept down: no-go)"; fi
else
  # hostapd: keep the NM connection (for --backend nm), down and never autoconnecting
  if con_exists; then
    nm_connection no
    if con_active; then nmcli connection down "$CON" >/dev/null; changed "$CON (NetworkManager AP) down"; fi
  fi
  printf '[keyfile]\n# K1000: the AP on %s runs on hostapd (box/net/setup-ap.sh --backend hostapd)\nunmanaged-devices=interface-name:%s\n' "$IFACE" "$IFACE" > "$TMP/unmanaged.conf"
  if put "$UNMANAGED_CONF" "$TMP/unmanaged.conf" 0644; then
    nmcli general reload conf >/dev/null 2>&1 || systemctl reload NetworkManager || true
    nmcli device set "$IFACE" managed no >/dev/null 2>&1 || true
    changed "$IFACE unmanaged by NetworkManager ($UNMANAGED_CONF)"
    RESTART_AP=1
  fi
  if [ "$BAND" = a ]; then hw_mode=a; else hw_mode=g; fi
  {
    printf '# K1000 AP (hostapd backend): generated by box/net/setup-ap.sh, 0600 (the password). Do not edit.\n'
    printf 'interface=%s\ndriver=nl80211\nctrl_interface=%s\nctrl_interface_group=0\n' "$IFACE" "$HOSTAPD_RUN"
    printf 'ssid=%s\nutf8_ssid=1\ncountry_code=%s\nieee80211d=1\n' "$SSID" "$COUNTRY"
    printf 'hw_mode=%s\nchannel=%s\nieee80211n=1\nwmm_enabled=1\n' "$hw_mode" "$CHANNEL"
    [ "$BAND" = a ] && printf 'ieee80211ac=1\n'
    printf 'auth_algs=1\nignore_broadcast_ssid=0\nwpa=2\nwpa_key_mgmt=WPA-PSK\nrsn_pairwise=CCMP\n'
    printf 'wpa_passphrase=%s\n' "$psk"
    printf '# ieee80211w is deliberately NOT set: PMF off (advertising PMF drops clients on this chip)\n'
  } > "$TMP/hostapd.conf"
  if put "$HOSTAPD_CONF" "$TMP/hostapd.conf" 0600; then changed "wrote $HOSTAPD_CONF ($SSID, $hw_mode ch $CHANNEL)"; RESTART_AP=1; fi
  [ $AP_ENV_NEW = 0 ] || RESTART_AP=1
  {
    printf '# K1000 AP DHCP + DNS (hostapd backend): generated by box/net/setup-ap.sh. Do not edit.\n'
    printf 'interface=%s\nbind-dynamic\nexcept-interface=lo\nlisten-address=%s\n' "$IFACE" "$IP"
    printf 'no-resolv\nno-hosts\npid-file=\n'
    printf 'dhcp-range=10.42.0.10,10.42.0.250,255.255.255.0,12h\n'
    printf 'dhcp-option=option:router,%s\ndhcp-option=option:dns-server,%s\n' "$IP" "$IP"
    printf 'dhcp-authoritative\ndhcp-leasefile=%s\n' "$LEASES_HOSTAPD"
    printf 'conf-file=%s\n' "$DNS_DST"
  } > "$TMP/ap-dnsmasq.conf"
  if put "$APDNS_CONF" "$TMP/ap-dnsmasq.conf" 0644; then changed "wrote $APDNS_CONF"; RESTART_AP=1; fi
  if command -v dnsmasq >/dev/null && ! dnsmasq --test --conf-file="$APDNS_CONF" >/dev/null 2>&1; then
    die "$APDNS_CONF does not pass 'dnsmasq --test'"
  fi
  UNITS_NEW=0
  for u in "$HOSTAPD_UNIT" "$APDNS_UNIT"; do
    if put "$UNIT_DIR/$u" "$SRC/$u" 0644; then changed "installed $UNIT_DIR/$u"; UNITS_NEW=1; fi
  done
  # The AP follows its interface: starts once it exists, stops if it goes (a dongle unplugged),
  # never a restart loop on a missing one. The device unit's name is systemd-escaped ("-" = \x2d).
  dev_unit="sys-subsystem-net-devices-${IFACE//-/\\x2d}.device"
  printf '# K1000 (box/net/setup-ap.sh): the AP runs only while %s exists.\n[Unit]\nBindsTo=%s\nAfter=%s\n' \
    "$IFACE" "$dev_unit" "$dev_unit" > "$TMP/iface.conf"
  if put "$DROPIN" "$TMP/iface.conf" 0644; then changed "wrote $DROPIN (bound to $IFACE)"; UNITS_NEW=1; fi
  [ $UNITS_NEW = 0 ] || { systemctl daemon-reload; RESTART_AP=1; }
  if [ "$STATE" = up ]; then
    for u in "$HOSTAPD_UNIT" "$APDNS_UNIT"; do
      if ! unit_enabled "$u"; then systemctl enable "$u" >/dev/null 2>&1; changed "enabled $u"; fi
    done
  else
    hostapd_off                           # kept down (no-go): installed, current, not running
    release_prev_iface
  fi
  unit_active "$HOSTAPD_UNIT" || HANDOVER=1
fi

# 7. what the box screen shows and the next run keeps (no secret in here)
if [ "$BAND" = a ]; then band_label="5 GHz · ch $CHANNEL"; else band_label="2.4 GHz · ch $CHANNEL"; fi
[ "$BACKEND" = hostapd ] && band_label="$band_label · hostapd"
info=$(printf 'ssid=%s\nband=%s\nchannel=%s\nlabel=%s\naddress=%s\niface=%s\nbackend=%s\nportal=%s\nssh=%s\nstate=%s\n' \
  "$SSID" "$BAND" "$CHANNEL" "$band_label" "$IP" "$IFACE" "$BACKEND" "$PORTAL" "$SSH" "$STATE")
if [ "$(cat "$INFO_FILE" 2>/dev/null || true)" != "$info" ]; then
  install -d -m 0755 "$(dirname "$INFO_FILE")"
  printf '%s\n' "$info" > "$INFO_FILE.tmp"
  chmod 0644 "$INFO_FILE.tmp"
  mv -f "$INFO_FILE.tmp" "$INFO_FILE"
  changed "wrote $INFO_FILE ($SSID, $band_label, $IFACE, portal $PORTAL, SSH $SSH, $STATE)"
fi

# 8. up (a re-activation applies a changed profile or DNS file; phones reconnect by themselves)
if [ "$STATE" = down ]; then
  note "AP kept DOWN (the no-go, $INFO_FILE state=down): everything is current, nothing on the air."
  note "Back on the air: sudo $0 --up"
elif [ $DO_UP = 1 ]; then
  if [ "$BACKEND" = nm ]; then
    if ! con_active || [ $RESTART_AP = 1 ]; then
      # (exit 4 from here on: steps 1-7 may have changed things; the count line says how many)
      fence_loaded || { note "ERROR: the fence is not loaded: the AP is never up without it (journalctl -u $FENCE_UNIT)"; note "$CHANGES changes"; exit 4; }
      note "bringing $CON up on $IFACE (phones on it reconnect in a few seconds)"
      if ! nmcli --wait 30 connection up "$CON" >/dev/null; then
        note "ERROR: $CON did not come up. Look at: journalctl -u NetworkManager -n 60 --no-pager"
        band_hint
        note "$CHANGES changes"
        exit 4
      fi
      changed "$CON is up"
    fi
  elif ! unit_active "$HOSTAPD_UNIT" || [ $RESTART_AP = 1 ]; then
    note "starting $HOSTAPD_UNIT on $IFACE"
    rm -f "$GUARD_MARK"
    release_prev_iface
    systemctl reset-failed "$HOSTAPD_UNIT" "$APDNS_UNIT" >/dev/null 2>&1 || true   # its start limit
    if [ $HANDOVER = 1 ]; then
      # the first hostapd start after NetworkManager/wpa_supplicant had the chip leaves its
      # firmware stale (every association refused, status 38): a second full start heals it
      systemctl restart "$HOSTAPD_UNIT" || true
      sleep "$SETTLE_S"
      note "handover from NetworkManager: starting hostapd a second time"
    fi
    if ! systemctl restart "$HOSTAPD_UNIT" "$APDNS_UNIT"; then
      note "ERROR: hostapd did not start. Look at: journalctl -u $HOSTAPD_UNIT -n 60 --no-pager"
      band_hint
      note "$CHANGES changes"
      exit 4
    fi
    if ! why=$(hostapd_check); then
      note "ERROR: the AP did not stay up: $why. Look at: journalctl -u $HOSTAPD_UNIT -n 60 --no-pager"
      band_hint
      note "$CHANGES changes"
      exit 4
    fi
    changed "$HOSTAPD_UNIT and $APDNS_UNIT are up"
  fi
  sleep 1
  if ! ip -4 -o addr show dev "$IFACE" | grep -q " $IP/"; then
    note "WARNING: $IFACE does not hold $IP yet"
  fi
  # the AP's DNS answers (a tiny DNS query; no dig needed)
  if command -v python3 >/dev/null; then
    dns=$(python3 - "$IP" <<'PY' 2>/dev/null || true
import socket, struct, sys
q = struct.pack(">HHHHHH", 0x1000, 0x0100, 1, 0, 0, 0)
q += b"".join(bytes([len(p)]) + p for p in b"k1000.home.arpa".split(b".")) + b"\0"
q += struct.pack(">HH", 1, 1)
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(2)
s.sendto(q, (sys.argv[1], 53))
d = s.recv(512)
print(socket.inet_ntoa(d[-4:]) if struct.unpack(">H", d[6:8])[0] else "no answer")
PY
)
    if [ "$dns" = "$IP" ]; then note "DNS ok (k1000.home.arpa -> $IP)"; else note "WARNING: the AP's DNS answered '${dns:-nothing}'"; fi
  fi
  fence_loaded || note "WARNING: the fence is not loaded: sudo systemctl reload-or-restart $FENCE_UNIT"
fi

if [ "$BAND" = a ] && [ "$COUNTRY" = FR ] && [ "$CHANNEL" -le 64 ]; then
  note "note: 5 GHz channel $CHANNEL is indoor-only in the EU; outdoors use: sudo $0 --band bg"
fi
note "SSID $SSID on $IFACE ($BACKEND, $STATE), portal $PORTAL, SSH from the AP $SSH; password in $PSK_FILE (box screen: SETUP > Phone), page https://$IP/"
note "$CHANGES changes"
