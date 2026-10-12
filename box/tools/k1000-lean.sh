#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# K1000 lean OS (DECISIONS.md D-006): converge a Raspberry Pi OS trixie arm64 card, desktop or Lite,
# to what the K1000 box needs, without the services that slow its boot or wake up during a show.
# Idempotent: a re-run with nothing left to do changes nothing and ends with "0 changes".
#
#   sudo ./k1000-lean.sh                 apply every step, in order
#   sudo ./k1000-lean.sh --only desktop  apply some steps only (comma list): one change set per reboot
#   sudo ./k1000-lean.sh --dry-run       print what would change, change nothing (apt removals are
#                                        still simulated and checked)
#   ./k1000-lean.sh --check              is the card safe to power-cycle? (read-only; exit 1 = NO)
#   ./k1000-lean.sh --list               the steps
#
# A real run detaches from the terminal: it re-runs itself as a transient systemd unit
# (k1000-lean-<time>) and follows its log, so a dropped SSH session or a client timeout can no
# longer kill apt/dpkg halfway through a purge (sudo's use_pty tears the command down with the
# session). Losing the session only stops the following: the log is /var/backups/k1000-lean/
# run-<time>.log, its exit status run-<time>.rc. K1000_LEAN_FOREGROUND=1 runs in place.
#
# Steps (all of them take effect at the next boot; power-cycle and check the box after each set):
#   keep      apt-mark manual every installed package K1000 needs, so no autoremove can take one
#   desktop   purge the desktop: the rpd-* metapackages of the desktop image, the desktop leftovers
#             that are installed (Lite: only rpi-connect-lite), then autoremove; guarded (below),
#             and checked after (--check) when anything was removed
#   services  cloud-init off (only once k1000-console.service has run at this boot and found the
#             panel), NetworkManager-wait-online, the EEPROM update check at boot, e2scrub_reap
#   timers    apt-daily, apt-daily-upgrade, man-db, e2scrub_all (no internet at gigs; no random
#             apt/man-db work in the middle of a show)
#   swap      zram only: no 2 GB swap file re-made on the SD card at every boot, no zram writeback
#             to the SD card three hours into a show
#   boot      cmdline.txt: quiet, logo.nologo, no splash/plymouth arguments; config.txt:
#             disable_splash=1. Both files are backed up first; cmdline.txt stays one line.
#
# The guard: every apt removal is simulated first (apt-get -s), the plan is saved under $STATE, and
# it is refused (nothing removed, exit 1) when it names a package of the KEEP list below or one
# matching CRITICAL_RE (boot, kernel, firmware, SSH, network, Wi-Fi, systemd, libc, Python, ALSA).
#
# The check (--check, and after every real run that changed something): dpkg has no half-done
# package; the boot packages are installed; each initramfs the firmware loads is byte-identical
# to /boot/initrd.img-* and has its /init (plymouth's removal rebuilds them, and raspi-firmware's
# hook copies them onto /boot/firmware in place); cmdline.txt is one line with this boot's root=
# and rootwait; kernel8.img and config.txt are there; i2c-dev is loaded at boot (the knobs).
#
# What it never does: touch root=/fstab/the initramfs configuration, kernel or firmware packages,
# the EEPROM configuration, sshd, sudoers, users, NetworkManager connections or the Wi-Fi radio;
# apt upgrade; reboot. Undo for each change is printed where it happens.
#
# K1000_BOOT_DIR overrides /boot/firmware; K1000_LEAN_STATE overrides /var/backups/k1000-lean (plans,
# backups, run logs); K1000_LEAN_ETC overrides /etc, K1000_LEAN_INITRD_DIR /boot and
# K1000_LEAN_PROC_CMDLINE /proc/cmdline (tests only).
set -euo pipefail

BOOT_DIR=${K1000_BOOT_DIR:-/boot/firmware}
STATE=${K1000_LEAN_STATE:-/var/backups/k1000-lean}
ETC=${K1000_LEAN_ETC:-/etc}
INITRD_DIR=${K1000_LEAN_INITRD_DIR:-/boot}
PROC_CMDLINE=${K1000_LEAN_PROC_CMDLINE:-/proc/cmdline}
STEPS=(keep desktop services timers swap boot)
ARGS=("$@")

# Everything K1000 needs installed. The guard refuses a removal plan that names one of these.
KEEP=(
  # boot, kernel, firmware, Raspberry Pi tools (vcgencmd for the soak and bring-up tools)
  raspi-firmware linux-image-rpi-v8 linux-image-rpi-2712 initramfs-tools raspberrypi-sys-mods
  rpi-eeprom raspi-config raspi-utils raspi-utils-core firmware-brcm80211 wireless-regdb
  # system and remote access
  systemd systemd-sysv udev sudo openssh-server openssh-client ca-certificates curl rsync cron kmod
  # network, and the phone's access point (D-007: hostapd + dnsmasq on wlan0, the nft fence;
  # NetworkManager's AP mode is the alternative backend)
  network-manager wpasupplicant dnsmasq-base iw rfkill nftables iproute2 hostapd
  # audio (camilladsp links libasound; aplay/arecord probe the cards), knobs, overlay compiler
  alsa-utils libasound2t64 i2c-tools usbutils device-tree-compiler
  # the k1000 service, its screen fonts, its tests
  python3 python3-websockets python3-yaml python3-numpy python3-pil python3-smbus2
  python3-libgpiod python3-qrcode python3-pytest fonts-dejavu-core
)
# What any K1000 card must still have before it is power-cycled (--check): boot, login, network.
ESSENTIAL=(raspi-firmware linux-image-rpi-v8 initramfs-tools raspberrypi-sys-mods systemd udev kmod sudo
           openssh-server network-manager wpasupplicant python3)
# ... and every package these patterns match (extended regex, whole name).
CRITICAL_RE='^(openssh-.*|ssh|network-manager|libnm0|systemd|systemd-.*|libsystemd.*|udev|libudev1|sudo|libc6|libc-bin|python3|python3-minimal|python3\.[0-9]+|python3\.[0-9]+-minimal|libpython3(\.[0-9]+)?(-minimal|-stdlib)?|raspi-firmware|linux-image-.*|initramfs-tools(-core|-bin)?|raspberrypi-sys-mods|rpi-eeprom|dnsmasq-base|wpasupplicant|iw|rfkill|alsa-.*|libasound.*|firmware-brcm80211|wireless-regdb|dbus|dbus-daemon|dbus-bin|dbus-system-bus-common|libdbus-1-3|kmod|e2fsprogs|util-linux|mount|base-files|coreutils|bash|dash|login|passwd|cron|nftables|iproute2|zstd|busybox|cpio|klibc-utils|libklibc)$'

# The desktop image = Lite + these ten metapackages (pi-gen stage3 + stage4).
RPD=(rpd-preferences rpd-theme rpd-wayland-core rpd-x-core rpd-applications rpd-developer
     rpd-graphics rpd-utilities rpd-wayland-extras rpd-x-extras)
# Purged too when installed. pinentry-gnome3 alone keeps GTK 3/4, mesa and x11 libraries (gpg-agent
# is happy with pinentry-curses); rpi-connect(-lite) is a remote-access agent a gig box never uses.
LEFTOVERS=(pinentry-gnome3 rpi-connect rpi-connect-lite chromium firefox vlc lightdm labwc wayfire
           pipewire pipewire-pulse wireplumber cups cups-daemon plymouth plymouth-themes
           rpd-plym-splash packagekit realvnc-vnc-server wayvnc)
# apt keeps any installed package that another one merely SUGGESTS (man-db suggests a web browser,
# so chromium would stay). A Lite install never had them; this option lets autoremove see that.
APT_OPTS=(-o APT::AutoRemove::SuggestsImportant=false)

DRY=0 ONLY="" CHECK=0
usage() { awk 'NR > 2 && /^#/ { sub(/^# ?/, ""); print; next } NR > 2 { exit }' "$0"; }
while [ $# -gt 0 ]; do
  case $1 in
    --dry-run) DRY=1 ;;
    --check) CHECK=1 ;;
    --only) [ $# -ge 2 ] || { echo "k1000-lean: --only needs a step list" >&2; exit 2; }; ONLY=$2; shift ;;
    --only=*) ONLY=${1#--only=} ;;
    --list) printf '%s\n' "${STEPS[@]}"; exit 0 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "k1000-lean: unknown option '$1' (try --help)" >&2; exit 2 ;;
  esac
  shift
done

CHANGES=0
note() { printf 'k1000-lean: %s\n' "$*"; }
die() { printf 'k1000-lean: ERROR: %s\n' "$*" >&2; exit 1; }
# act "what it does (undo: ...)" cmd args...: run it (or only say it, in a dry run) and count it.
act() {
  local what=$1; shift
  CHANGES=$((CHANGES + 1))
  if [ $DRY = 1 ]; then note "would: $what"; return 0; fi
  "$@"
  note "$what"
}
# What dpkg has installed and what apt marked automatic, read once (refresh_pkgs again after apt).
declare -A INST=() AUTO=()
refresh_pkgs() {
  local p st
  INST=() AUTO=()
  # shellcheck disable=SC2016  # ${Package} is dpkg-query's field, not a shell variable
  while read -r p st; do if [ "$st" = ii ]; then INST[$p]=1; fi; done \
    < <(dpkg-query -W -f='${Package} ${db:Status-Abbrev}\n' 2>/dev/null)
  while read -r p; do AUTO[${p%%:*}]=1; done < <(apt-mark showauto 2>/dev/null)
}
installed() { [ -n "${INST[$1]:-}" ]; }
TS=$(date +%Y%m%d-%H%M%S)

# --- the guard -----------------------------------------------------------------------------------
# guard_plan FILE: FILE lists package names, one per line (an apt-get -s plan). Prints every name
# that must survive and returns 1 if there is one.
guard_plan() {
  local bad
  bad=$( { grep -Ex "$CRITICAL_RE" "$1" || true; grep -Fx -f <(printf '%s\n' "${KEEP[@]}") "$1" || true; } | sort -u)
  if [ -n "$bad" ]; then
    printf 'k1000-lean: REFUSED: the plan removes packages that must survive:\n' >&2
    printf '  %s\n' "${bad//$'\n'/$'\n'  }" >&2
    return 1
  fi
}

# plan_of ARGS...: names of the packages `apt-get -s ARGS` would remove, one per line.
plan_of() {
  local out
  out=$(apt-get -s "${APT_OPTS[@]}" "$@" 2>&1) || { printf '%s\n' "$out" >&2; die "apt-get -s $* failed"; }
  printf '%s\n' "$out" | awk '$1 == "Purg" || $1 == "Remv" { print $2 }' | LC_ALL=C sort -u
}

# remove_guarded LABEL ARGS...: simulate `apt-get ARGS`, save the plan, refuse it if it touches
# the KEEP set, else run it. Lists of what was removed stay in $STATE.
remove_guarded() {
  local label=$1; shift
  local plan n
  plan=$(mktemp)
  plan_of "$@" > "$plan"
  n=$(grep -c . "$plan" || true)
  if [ "$n" = 0 ]; then rm -f "$plan"; return 0; fi
  if ! guard_plan "$plan"; then
    if [ $DRY = 0 ]; then mkdir -p "$STATE"; cp "$plan" "$STATE/REFUSED-$label-$TS.txt"; fi
    rm -f "$plan"
    die "$label: plan refused (saved as $STATE/REFUSED-$label-$TS.txt unless --dry-run); nothing was removed"
  fi
  if [ $DRY = 1 ]; then
    note "would: $label: purge $n packages (plan checked: nothing K1000 needs):"
    fmt -w 100 "$plan" | sed 's/^/    /'
    CHANGES=$((CHANGES + 1)); rm -f "$plan"; return 0
  fi
  mkdir -p "$STATE"
  cp "$plan" "$STATE/$label-$TS.txt"
  backup_boot "$label"
  REMOVED=1
  DEBIAN_FRONTEND=noninteractive apt-get -y "${APT_OPTS[@]}" "$@"
  refresh_pkgs
  CHANGES=$((CHANGES + 1))
  note "$label: purged $n packages (list: $STATE/$label-$TS.txt; undo: apt-get install <those names>)"
  rm -f "$plan"
}

# backup_boot LABEL: cmdline.txt and config.txt next to themselves and in $STATE, and the
# initramfs images in $STATE (a package removal may rebuild them).
backup_boot() {
  local f
  mkdir -p "$STATE/boot-$1-$TS"
  for f in cmdline.txt config.txt; do
    [ -f "$BOOT_DIR/$f" ] || continue
    cp "$BOOT_DIR/$f" "$BOOT_DIR/$f.k1000-lean-$1-$TS"
    cp "$BOOT_DIR/$f" "$STATE/boot-$1-$TS/$f"
  done
  for f in "$BOOT_DIR"/initramfs*; do
    if [ -f "$f" ]; then cp "$f" "$STATE/boot-$1-$TS/"; fi
  done
  note "boot files backed up: $BOOT_DIR/*.k1000-lean-$1-$TS and $STATE/boot-$1-$TS/"
}

# --- step: keep ----------------------------------------------------------------------------------
KEEP_DONE=0
step_keep() {
  local p auto=() missing=()
  [ $KEEP_DONE = 0 ] || return 0
  KEEP_DONE=1
  for p in "${KEEP[@]}"; do
    if ! installed "$p"; then missing+=("$p"); continue; fi
    if [ -n "${AUTO[$p]:-}" ]; then auto+=("$p"); fi
  done
  [ ${#missing[@]} = 0 ] || note "not installed (install.sh/bootstrap.sh installs what K1000 needs): ${missing[*]}"
  [ ${#auto[@]} = 0 ] || act "marked manual: ${auto[*]} (undo: apt-mark auto ${auto[*]})" \
    mark_manual "${auto[@]}"
}
mark_manual() { apt-mark manual "$@" >/dev/null; for p in "$@"; do unset "AUTO[$p]"; done; }

# --- step: desktop -------------------------------------------------------------------------------
step_desktop() {
  local p targets=() had_quiet=0 kept=()
  step_keep       # the marks come before any removal, whatever --only says
  for p in "${RPD[@]}" "${LEFTOVERS[@]}"; do if installed "$p"; then targets+=("$p"); fi; done
  for p in "${KEEP[@]}"; do if installed "$p"; then kept+=("$p"); fi; done
  if [ -n "$(dpkg --audit 2>&1)" ]; then die "dpkg reports half-installed packages (dpkg --audit): fix that first"; fi
  if cmdline_has quiet; then had_quiet=1; fi
  if [ ${#targets[@]} -gt 0 ]; then
    remove_guarded desktop purge --autoremove "${targets[@]}"
  fi
  remove_guarded autoremove autoremove --purge
  [ $DRY = 0 ] || return 0
  [ $REMOVED = 1 ] || return 0     # nothing removed: nothing to check (a converged card)
  # rpd-plym-splash's prerm strips " quiet" along with "splash": keep the kernel quiet (the boot
  # step ensures it too, but this step must not leave a noisier boot behind on its own).
  if [ $had_quiet = 1 ] && ! cmdline_has quiet; then
    cmdline_edit add quiet
    note "cmdline.txt: quiet put back (the splash package's removal took it out)"
  fi
  check_after_removal "${kept[@]}"
}
REMOVED=0

# check_after_removal PKG...: every package given (KEEP, installed before) still is, and the card
# passes the power-cycle check (initramfs rebuilt when plymouth goes, cmdline.txt edited by the
# splash package's removal).
check_after_removal() {
  local p lost=()
  for p in "$@"; do installed "$p" || lost+=("$p"); done
  [ ${#lost[@]} = 0 ] || die "packages K1000 needs were removed: ${lost[*]} (apt-get install them NOW, before any power cycle)"
  boot_check || die "the card fails the power-cycle check (above): fix that BEFORE any power cycle (boot files: $STATE/boot-*/)"
}

# --- the power-cycle check -------------------------------------------------------------------------
# has_init IMG: IMG lists an /init. lsinitramfs goes to a file, never into `grep -q`: grep -q exits
# at the first match, lsinitramfs then dies writing into the closed pipe, and pipefail turned that
# into "no /init" on every healthy initramfs.
has_init() {
  local tmp rc=1
  tmp=$(mktemp)
  if lsinitramfs "$1" > "$tmp" 2>/dev/null && grep -qx 'init' "$tmp"; then rc=0; fi
  rm -f "$tmp"
  return $rc
}
# boot_problems: one line per reason the next boot could fail (or the knobs stay dead); nothing
# when the card is fine. Read-only.
boot_problems() {
  local p lost=() fl dst src audit f=$BOOT_DIR/cmdline.txt words roots now
  audit=$(dpkg --audit 2>&1 || true)
  [ -z "$audit" ] || echo "dpkg --audit: a package is half-installed or half-removed (an interrupted apt?): finish it with: sudo dpkg --configure -a"
  for p in "${ESSENTIAL[@]}"; do installed "$p" || lost+=("$p"); done
  [ ${#lost[@]} = 0 ] || echo "not installed: ${lost[*]} (sudo apt-get install ${lost[*]})"
  # The firmware loads initramfs8 (Pi 4, kernel8.img) / initramfs_2712 (Pi 5), copies of the
  # newest /boot/initrd.img-*-rpi-v8 / -rpi-2712 (raspi-firmware's initramfs hook).
  command -v lsinitramfs >/dev/null || echo "lsinitramfs not found: the initramfs cannot be checked"
  for fl in v8 2712; do
    if [ $fl = v8 ]; then dst=$BOOT_DIR/initramfs8; else dst=$BOOT_DIR/initramfs_2712; fi
    src=$(find "$INITRD_DIR" -maxdepth 1 -name "initrd.img-*-rpi-$fl" 2>/dev/null | sort -V | tail -n 1)
    if [ -z "$src" ] && [ ! -e "$dst" ]; then continue; fi
    if [ -z "$src" ]; then echo "$dst has no initrd.img-*-rpi-$fl in $INITRD_DIR to compare with"; continue; fi
    if [ ! -e "$dst" ]; then echo "$dst is missing: sudo cp $src $dst.new && sudo mv $dst.new $dst"; continue; fi
    if ! cmp -s "$src" "$dst"; then
      echo "$dst differs from $src (an interrupted copy?): sudo cp $src $dst.new && sudo mv $dst.new $dst"
      continue
    fi
    if command -v lsinitramfs >/dev/null && ! has_init "$dst"; then
      echo "$dst has no /init: restore it (and $src) from $STATE/boot-*/"
    fi
  done
  [ -s "$BOOT_DIR/kernel8.img" ] || echo "$BOOT_DIR/kernel8.img is missing or empty"
  [ -s "$BOOT_DIR/config.txt" ] || echo "$BOOT_DIR/config.txt is missing or empty"
  if [ ! -s "$f" ]; then
    echo "$f is missing or empty"
  else
    [ "$(grep -c '' "$f")" -le 1 ] || echo "$f has more than one line (the firmware reads the first only)"
    words=$(tr -s '[:blank:]' '\n' < "$f")
    roots=$(grep '^root=' <<<"$words" || true)
    now=$( { tr -s '[:blank:]' '\n' < "$PROC_CMDLINE"; } 2>/dev/null | grep '^root=' | tail -n 1 || true)
    if [ -z "$roots" ] || [ "$(grep -c . <<<"$roots")" != 1 ]; then
      echo "$f: not exactly one root= argument"
    elif [ -n "$now" ] && [ "$roots" != "$now" ]; then
      echo "$f has $roots, but this boot came up with $now"
    fi
    grep -x rootwait <<<"$words" >/dev/null || echo "$f has no rootwait"
  fi
  # /dev/i2c-1 (the knob board) needs i2c-dev, which nothing loads on demand (install.sh --boot).
  grep -Eqs '^[[:space:]]*i2c[-_]dev([[:space:]]|$)' "$ETC/modules" "$ETC"/modules-load.d/*.conf \
    || echo "i2c-dev is not loaded at boot (no line in $ETC/modules-load.d/): the knobs would be dead; run install.sh --boot"
}
# boot_check: the power-cycle check, said out loud. Returns 1 when the card must not be power-cycled.
boot_check() {
  local problems
  problems=$(boot_problems)
  if [ -n "$problems" ]; then
    printf 'k1000-lean: check FAILED, do NOT power-cycle:\n' >&2
    printf '%s\n' "$problems" | sed 's/^/  - /' >&2
    return 1
  fi
  note "check: dpkg clean, boot packages installed, initramfs = /boot copies with /init, cmdline.txt one line with this boot's root=, i2c-dev at boot: safe to power-cycle"
}

# --- step: services ------------------------------------------------------------------------------
disable_unit() {   # disable_unit UNIT WHY: only when plainly enabled
  if [ "$(systemctl is-enabled "$1" 2>/dev/null || true)" = enabled ]; then
    act "disabled $1: $2 (undo: systemctl enable $1)" systemctl disable --quiet "$1"
  fi
}
# console_guard_proven: k1000-console.service is enabled, was started by THIS boot (in its first
# 2 minutes, not by a later install.sh --start), exited 0 and logged that it found the panel.
# Sets GUARD_WHY when not. Enabled alone proves nothing: installed by the same run (bootstrap), it
# has never run at a boot.
GUARD_WHY=""
console_guard_proven() {
  local props active status start log
  if [ "$(systemctl is-enabled k1000-console.service 2>/dev/null || true)" != enabled ]; then
    GUARD_WHY="k1000-console.service is not enabled (run install.sh first)"; return 1
  fi
  props=$(systemctl show -p ActiveState -p ExecMainStatus -p ExecMainStartTimestampMonotonic k1000-console.service 2>/dev/null || true)
  active=$(sed -n 's/^ActiveState=//p' <<<"$props")
  status=$(sed -n 's/^ExecMainStatus=//p' <<<"$props")
  start=$(sed -n 's/^ExecMainStartTimestampMonotonic=//p' <<<"$props")
  if [ "$active" != active ] || [ "$status" != 0 ] || ! [[ $start =~ ^[0-9]+$ ]] || [ "$start" = 0 ] \
     || [ "$start" -gt 120000000 ]; then
    GUARD_WHY="k1000-console.service has not run at this boot (power-cycle once with it installed, then run this again)"; return 1
  fi
  log=$(journalctl -b -u k1000-console.service -o cat --no-pager 2>/dev/null || true)
  if [[ $log != *"k1000-console: ZynScreen is fb"* ]]; then
    GUARD_WHY="k1000-console.service ran at this boot but did not find the panel (journalctl -b -u k1000-console)"; return 1
  fi
}
step_services() {
  if [ -d "$ETC/cloud" ] && [ ! -e "$ETC/cloud/cloud-init.disabled" ]; then
    if console_guard_proven; then
      act "cloud-init disabled: its four units held k1000 back ~2.6 s at every boot; the card is provisioned (undo: rm $ETC/cloud/cloud-init.disabled)" \
        touch "$ETC/cloud/cloud-init.disabled"
    else
      note "cloud-init left on: $GUARD_WHY. Without the guard, a faster boot starts k1000 before the panel registers and the console keeps drawing over the UI"
    fi
  fi
  disable_unit NetworkManager-wait-online.service "4.3 s at every boot for nothing K1000 starts (the units order on network.target at most)"
  disable_unit rpi-eeprom-update.service "no bootloader update staged at a gig boot (update by hand: sudo rpi-eeprom-update -a)"
  disable_unit e2scrub_reap.service "removes stale LVM e2scrub snapshots; the box has no LVM"
  disable_unit glamor-test.service "desktop X/GL check"
  disable_unit ModemManager.service "no modem"
  disable_unit triggerhappy.service "no hotkey daemon needed"
  local still
  still=$(systemctl list-dependencies --reverse --plain --no-legend network-online.target 2>/dev/null \
          | sed 's/^[[:space:]]*//' | grep -v '^network-online.target$' | tr '\n' ' ' || true)
  [ -z "$still" ] || note "still pulling network-online.target (now reached without waiting for an address; none is a K1000 unit): $still"
}

# --- step: timers --------------------------------------------------------------------------------
step_timers() {
  local t
  for t in apt-daily.timer apt-daily-upgrade.timer man-db.timer e2scrub_all.timer; do
    if [ "$(systemctl is-enabled "$t" 2>/dev/null || true)" = enabled ]; then
      act "disabled $t: no unattended apt/man-db/e2scrub work on a gig box (undo: systemctl enable --now $t)" \
        systemctl disable --now --quiet "$t"
    fi
  done
}

# --- step: swap ----------------------------------------------------------------------------------
SWAP_CONF_BODY='# K1000 (box/tools/k1000-lean.sh): swap in RAM only (zram). The box has 8 GB for a few hundred MB
# of work: no swap file on the SD card (Raspberry Pi OS re-sizes and mkswaps a 2 GB /var/swap at
# every boot, 1.5 s) and no zram writeback to the SD card hours into a show. Applies at the next
# boot, which also deletes /var/swap. Undo: delete this file and power-cycle.
[Main]
Mechanism=zram'
step_swap() {
  local f=$ETC/rpi/swap.conf.d/50-k1000.conf
  [ -d "$ETC/rpi" ] || { note "no $ETC/rpi (rpi-swap not installed): swap left as it is"; return 0; }
  if [ -f "$f" ] && [ "$(cat "$f")" = "$SWAP_CONF_BODY" ]; then return 0; fi
  act "swap: zram only, no SD swap file ($f; applies at the next boot; undo: rm $f)" \
    write_file "$f" "$SWAP_CONF_BODY"
}
write_file() { mkdir -p "$(dirname "$1")"; printf '%s\n' "$2" > "$1"; }

# --- step: boot ----------------------------------------------------------------------------------
# cmdline_edit add|drop TOKEN: one whole-word kernel argument, the file kept as ONE line (with or
# without its final newline, as it was). Prints nothing and changes nothing when already so.
cmdline_edit() {
  local op=$1 tok=$2 f=$BOOT_DIR/cmdline.txt words=() w out=() found=0 nl=""
  [ "$(grep -c '' "$f")" -le 1 ] || die "$f has more than one line: fix it by hand first"
  read -ra words < "$f" || true
  [ ${#words[@]} -gt 0 ] || die "$f is empty"
  if [ "$(tail -c 1 "$f" | od -An -c | tr -d ' ')" = '\n' ]; then nl=$'\n'; fi
  for w in "${words[@]}"; do
    if [ "$w" = "$tok" ]; then found=1; if [ "$op" = drop ]; then continue; fi; fi
    out+=("$w")
  done
  if [ "$op" = add ]; then
    [ $found = 0 ] || return 0
    out+=("$tok")
  else
    [ $found = 1 ] || return 0
  fi
  printf '%s%s' "${out[*]}" "$nl" > "$f.k1000-new"
  mv "$f.k1000-new" "$f"
}
# cmdline_has TOKEN: TOKEN is one of cmdline.txt's arguments (a whole one, not a substring).
cmdline_has() {
  local words=()
  read -ra words < "$BOOT_DIR/cmdline.txt" 2>/dev/null || true
  [[ " ${words[*]} " == *" $1 "* ]]
}
step_boot() {
  local f=$BOOT_DIR/cmdline.txt c=$BOOT_DIR/config.txt todo=() t
  [ -f "$f" ] && [ -f "$c" ] || die "$BOOT_DIR has no cmdline.txt/config.txt"
  cmdline_has quiet || todo+=("add quiet")
  cmdline_has logo.nologo || todo+=("add logo.nologo")
  if installed plymouth; then
    cmdline_has plymouth.enable=0 || todo+=("add plymouth.enable=0")
  else
    for t in splash plymouth.ignore-serial-consoles; do
      if cmdline_has "$t"; then todo+=("drop $t"); fi
    done
  fi
  local splash=0
  grep -Eq '^[[:space:]]*disable_splash=1([[:space:]]|$)' "$c" || splash=1
  [ ${#todo[@]} = 0 ] && [ $splash = 0 ] && return 0
  if [ $DRY = 1 ]; then
    for t in "${todo[@]}"; do act "cmdline.txt: $t"; done
    [ $splash = 0 ] || act "config.txt: disable_splash=1 (no rainbow square on HDMI)"
    return 0
  fi
  backup_boot boot
  for t in "${todo[@]}"; do
    cmdline_edit "${t%% *}" "${t#* }"
    act "cmdline.txt: $t (undo: cp $f.k1000-lean-boot-$TS $f)" true
  done
  if [ $splash = 1 ]; then
    printf '\n[all]\n# K1000 (box/tools/k1000-lean.sh): no firmware rainbow splash on HDMI\ndisable_splash=1\n' >> "$c"
    act "config.txt: disable_splash=1 (undo: cp $c.k1000-lean-boot-$TS $c)" true
  fi
  note "boot files now (diff against $STATE/boot-boot-$TS/):"
  diff "$STATE/boot-boot-$TS/cmdline.txt" "$f" | sed 's/^/    /' || true
  diff "$STATE/boot-boot-$TS/config.txt" "$c" | sed 's/^/    /' || true
}

# --- detach from the terminal ----------------------------------------------------------------------
# follow LOG RCFILE UNIT: print LOG as it grows until RCFILE says the run ended (or UNIT is gone
# without one). Only reads: killing this (a lost SSH session) leaves the unit running.
follow() {
  local pos=0 n gone=0 chunk
  chunk=$(mktemp)
  while :; do
    tail -c +$((pos + 1)) "$1" > "$chunk" 2>/dev/null || true
    n=$(stat -c %s "$chunk")
    if [ "$n" -gt 0 ]; then cat "$chunk"; pos=$((pos + n)); fi
    [ ! -s "$2" ] || break
    if systemctl is-active --quiet "$3" 2>/dev/null; then gone=0; else gone=$((gone + 1)); fi
    [ $gone -lt 3 ] || break
    sleep 1
  done
  tail -c +$((pos + 1)) "$1" 2>/dev/null || true
  rm -f "$chunk"
}
# detach: re-run this very command as the transient unit k1000-lean-<time> (log and exit status
# in $STATE), follow it, and exit with its status.
detach() {
  local unit=k1000-lean-$TS log=$STATE/run-$TS.log rcf=$STATE/run-$TS.rc self e envs=() rc
  self=$(cd "$(dirname "$0")" && pwd -P)/$(basename "$0")
  command -v systemd-run >/dev/null || die "systemd-run not found (on a console on the box: K1000_LEAN_FOREGROUND=1 $0 ${ARGS[*]})"
  mkdir -p "$STATE"
  : > "$log"
  for e in K1000_BOOT_DIR K1000_LEAN_STATE K1000_LEAN_ETC K1000_LEAN_INITRD_DIR K1000_LEAN_PROC_CMDLINE; do
    if [ -n "${!e:-}" ]; then envs+=("--setenv=$e=${!e}"); fi
  done
  systemd-run --quiet --collect --unit="$unit" -p "StandardOutput=append:$log" -p "StandardError=append:$log" \
    "${envs[@]}" "--setenv=K1000_LEAN_RC=$rcf" "$self" "${ARGS[@]}" \
    || die "systemd-run failed: nothing was run"
  note "running as the systemd unit $unit: a lost SSH session no longer interrupts it"
  note "log: $log   running: systemctl is-active $unit   exit status (when done): $rcf"
  follow "$log" "$rcf" "$unit"
  [ -s "$rcf" ] || die "$unit ended without an exit status (killed?): read $log, then: sudo $self --check"
  rc=$(cat "$rcf")
  exit "$rc"
}

# --- main ----------------------------------------------------------------------------------------
main() {
  local s want
  if ! command -v apt-get >/dev/null || ! command -v systemctl >/dev/null; then
    die "apt-get/systemctl not found: this runs on the K1000 box only"
  fi
  if [ $CHECK = 1 ]; then
    refresh_pkgs
    boot_check || exit 1
    exit 0
  fi
  [ $DRY = 1 ] || [ "$(id -u)" = 0 ] || die "run as root (or with --dry-run): sudo $0"
  if [ -n "$ONLY" ]; then
    for s in ${ONLY//,/ }; do
      [[ " ${STEPS[*]} " == *" $s "* ]] || die "unknown step '$s' (steps: ${STEPS[*]})"
    done
  fi
  # A real run, from a terminal: as a systemd unit instead (already one: bootstrap's, or this one).
  if [ $DRY = 0 ] && [ -z "${INVOCATION_ID:-}" ] && [ "${K1000_LEAN_FOREGROUND:-0}" != 1 ]; then detach; fi
  # The detached run leaves its exit status for the terminal that follows it (detach).
  if [ -n "${K1000_LEAN_RC:-}" ]; then trap 'echo $? > "$K1000_LEAN_RC"' EXIT; fi
  refresh_pkgs
  for s in "${STEPS[@]}"; do
    want=1
    if [ -n "$ONLY" ] && [[ ",$ONLY," != *",$s,"* ]]; then want=0; fi
    [ $want = 1 ] || continue
    "step_$s"
  done
  if [ $DRY = 1 ]; then
    note "dry run: $CHANGES changes"
  elif [ $CHANGES = 0 ]; then
    note "0 changes"
  else
    boot_check || die "$CHANGES changes made, but the card fails the power-cycle check (above): fix that BEFORE any power cycle"
    note "$CHANGES changes (they take effect at the next boot: power-cycle, then check the box)"
  fi
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then main; fi
