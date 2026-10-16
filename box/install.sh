#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# K1000 installer: deploys box/ onto the Raspberry Pi. Idempotent; a re-run with nothing new
# changes nothing and ends with "0 changes". See README.md (same directory) for the full picture.
#
#   sudo ./install.sh             install/update files, enable the 4 units (+ the laptop-access
#                                 watcher and its root unit: installed, never enabled; k1000
#                                 brings the watcher). Starts NOTHING.
#   sudo ./install.sh --start     ... then (re)start camilladsp, k1000, camillagui, k1000-console
#                                 (audio drops ~1 s; never during a show)
#   sudo ./install.sh --boot      ... and boot-level setup, applied at the next reboot:
#                                 compile + install the ZynScreen overlay (dtc -@), make sure
#                                 config.txt loads it, add vt.global_cursor_default=0 to cmdline.txt;
#                                 I2C for the knob board (dtparam=i2c_arm=on, i2c-dev at boot)
#   sudo ./install.sh --headless  ... and boot to the console instead of the desktop, with PipeWire/
#                                 WirePlumber masked, so nothing but CamillaDSP opens the audio
#                                 interfaces; the desktop image's unused services (Bluetooth,
#                                 printing, NFS/rpcbind) disabled and the tty1 auto-login removed
#                                 (applied at the next reboot). The rest of the lean OS (desktop
#                                 purge, cloud-init, timers, swap, boot arguments) is
#                                 tools/k1000-lean.sh (D-006)
#   sudo ./install.sh --ap        ... and set up the phone's Wi-Fi access point "K1000" on wlan0 + its
#                                 fence (net/setup-ap.sh, D-007; first time: hostapd, 5 GHz ch 36,
#                                 portal off, SSH fenced off; later runs keep its choices, a no-go
#                                 `setup-ap.sh --down` included). eth0 is never touched. Takes
#                                 effect at once; the only way the AP's units are installed/updated
#   ./install.sh --deps           print the apt packages it may install, one per line (bootstrap.sh
#                                 runs apt-get update only when one is missing)
#
# Run it from a checkout of box/ or an rsync'd copy (e.g. /opt/k1000-src), never from /opt/k1000.prev.
# K1000_REVISION=<text> records what is deployed in /opt/k1000/REVISION (default: git describe,
#   else the source tree's own REVISION file, e.g. a rollback copy of /opt/k1000.prev).
# K1000_BOOT_DIR overrides /boot/firmware (only to test --boot on a copy).
# CAMILLAGUI_TARBALL=<path> installs CamillaGUI from a local tarball instead of downloading it.
# K1000_TLS_DIR=<dir> installs the phone page's HTTPS certificate from <dir> (k1000.crt, k1000.key,
#   k1000-ca.crt) into /etc/k1000/tls, root:k1000 0640. Never the CA's private key (ca.key): it
#   never leaves the laptop that holds it. Restart k1000 (between shows) to load a new certificate.
# apk/K1000-<version>.apk next to this script (the signed release APK, put there by the deploy:
#   never in git) goes to /opt/k1000/apk, the newest one only; the first-time page offers it.
#
# Why each step exists is said where it happens. What it never does: touch /etc/k1000/k1000.yaml
# once it exists (Thomas's settings), touch /var/lib/k1000 contents (live state; the one exception
# is removing the EMPTY coeffs/ folder that an earlier version created there), reboot.
set -euo pipefail

SRC=$(cd "$(dirname "$0")" && pwd -P)
BOOT_DIR=${K1000_BOOT_DIR:-/boot/firmware}
APP=/opt/k1000
PREV=/opt/k1000.prev
GUI=/opt/camillagui
UNITS=(camilladsp k1000 camillagui k1000-console k1000-usbwatch)
# Laptop access (docs/06-phone.md §11): the watcher, which k1000.service Wants=, and the root unit
# it starts on the box's request. Installed here, never enabled (nothing starts them at boot).
LAPTOP_UNITS=(k1000-laptop.path k1000-laptop.service)
AP_UNITS=(k1000-ap-fence k1000-ap-hostapd k1000-ap-dnsmasq)   # net/: set up by net/setup-ap.sh
# smbus2 for the knob board (D-008: python3-smbus is GPL-2.0-only), qrcode for the PHONE page's
# join codes and hostapd for the phone's access point (D-007, net/setup-ap.sh; installed even
# without --ap, because a venue has no internet. Inert until then: Debian's own hostapd.service
# needs /etc/hostapd/hostapd.conf, never created; K1000 runs k1000-ap-hostapd.service).
# fonts-dejavu-core (the UI's fonts) is not in Raspberry Pi OS Lite either; rsync, alsa-utils
# (aplay/arecord probe the cards) and usbutils (lsusb, M4 bring-up) are (the 2026-10-06 image),
# and stay listed so nothing depends on that.
APT_DEPS=(python3-websockets python3-smbus2 python3-libgpiod python3-yaml python3-pil python3-numpy
          python3-qrcode python3-pytest rsync alsa-utils fonts-dejavu-core usbutils hostapd)
# CamillaGUI v5.0.0-beta.2, linux aarch64. Pinned by hash: its binary reports "5.0.0-beta.1" in
# --version, so the version string cannot tell an upgrade from a no-op.
GUI_URL=https://github.com/HEnquist/camillagui-backend/releases/download/v5.0.0-beta.2/camillagui_linux_aarch64.tar.gz
GUI_TGZ_SHA256=854552f3e74a156d25117a1ecca2df44c94aefbeb62e1c47ff23b19d6b0cc8a2
GUI_BIN_SHA256=d993616caf6c67dc09b11a337d484ca2eca6c21c517305dc9d95df2f2e7bf14e

DO_START=0 DO_BOOT=0 DO_HEADLESS=0 DO_AP=0
for arg in "$@"; do
  case $arg in
    --ap) DO_AP=1 ;;
    --start) DO_START=1 ;;
    --boot) DO_BOOT=1 ;;
    --headless) DO_HEADLESS=1 ;;
    --deps) printf '%s\n' "${APT_DEPS[@]}" device-tree-compiler; exit 0 ;;   # dtc: --boot
    -h|--help) awk 'NR > 2 && /^#/ { sub(/^# ?/, ""); print; next } NR > 2 { exit }' "$0"; exit 0 ;;
    *) echo "install.sh: unknown option '$arg' (try --help)" >&2; exit 2 ;;
  esac
done

CHANGES=0
note() { printf 'k1000-install: %s\n' "$*"; }
changed() { CHANGES=$((CHANGES + 1)); note "$*"; }
die() { printf 'k1000-install: ERROR: %s\n' "$*" >&2; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# put SRC DST MODE: install a file only when its content differs, so re-runs keep mtimes and
# report nothing. MODE "-" = leave mode/owner alone (vfat /boot cannot chmod). Sets PUT_CHANGED.
PUT_CHANGED=0
put() {
  local src=$1 dst=$2 mode=$3
  PUT_CHANGED=0
  if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
    if [ "$mode" != "-" ] && [ "$(stat -c '%a %U:%G' "$dst")" != "$mode root:root" ]; then
      chmod "$mode" "$dst"; chown root:root "$dst"; changed "fixed mode of $dst"; PUT_CHANGED=1
    fi
    return 0
  fi
  mkdir -p "$(dirname "$dst")"
  if [ "$mode" = "-" ]; then cp "$src" "$dst"; else install -m "$mode" -o root -g root "$src" "$dst"; fi
  changed "installed $dst"
  PUT_CHANGED=1
}

# own_dir DIR [OWNER]: a directory, created or fixed only when needed. OWNER k1000 (default) =
# one k1000 writes (state, logs); root = one the services may read but never write.
own_dir() {
  local d=$1 o=${2:-k1000}
  if [ "$(stat -c '%U:%G %a' "$d" 2>/dev/null)" != "$o:$o 755" ]; then
    install -d -o "$o" -g "$o" -m 0755 "$d"
    changed "directory $d ($o:$o 755)"
  fi
}

# revision_of SRC: what SRC holds, for /opt/k1000/REVISION. git describe in a checkout; else the
# REVISION file a copy of /opt/k1000(.prev) carries (the README rollback reinstalls one), marked
# as reinstalled (never twice); else "unknown".
revision_of() {
  local src=$1 r
  if r=$(git -C "$src" describe --always --dirty 2>/dev/null) && [ -n "$r" ]; then
    printf '%s\n' "$r"
  elif [ -s "$src/REVISION" ] && r=$(head -n 1 "$src/REVISION") && [ -n "$r" ]; then
    printf '%s (reinstalled from %s)\n' "${r% (reinstalled from *}" "$src"
  else
    printf 'unknown (from %s)\n' "$src"
  fi
}

[ "$(id -u)" = 0 ] || die "run as root: sudo $0 $*"
command -v systemctl >/dev/null || die "systemd not found: this runs on the K1000 box only"
case $SRC in
  "$PREV"|"$PREV"/*) die "do not run from $PREV (it is rewritten by this script); copy it first: sudo rm -rf /tmp/k1000-rollback && sudo cp -a $PREV /tmp/k1000-rollback && sudo /tmp/k1000-rollback/install.sh" ;;
esac
[ -d "$SRC/k1000" ] && [ -d "$SRC/systemd" ] || die "$SRC does not look like box/ (no k1000/ or systemd/)"

# --- 0. the phone certificate (K1000_TLS_DIR), checked before anything changes -----------------
# All three files present and the key matching the certificate, or nothing is installed at all: a
# failed scp must not leave new code under old units (step 3 runs long before step 6). Step 4b
# only copies them.
TLS_FILES=(k1000.crt k1000.key k1000-ca.crt)
if [ -n "${K1000_TLS_DIR:-}" ]; then
  for f in "${TLS_FILES[@]}"; do
    [ -f "$K1000_TLS_DIR/$f" ] || die "K1000_TLS_DIR=$K1000_TLS_DIR has no $f (nothing installed)"
  done
  if command -v openssl >/dev/null; then
    crt_pub=$(openssl x509 -in "$K1000_TLS_DIR/k1000.crt" -noout -pubkey 2>/dev/null || true)
    key_pub=$(openssl pkey -in "$K1000_TLS_DIR/k1000.key" -pubout 2>/dev/null || true)
    if [ -z "$crt_pub" ] || [ "$crt_pub" != "$key_pub" ]; then
      die "$K1000_TLS_DIR/k1000.key does not match its k1000.crt, or one is unreadable (nothing installed)"
    fi
  else
    note "WARNING: no openssl: cannot check that k1000.key matches k1000.crt"
  fi
  [ ! -e "$K1000_TLS_DIR/ca.key" ] || note "WARNING: $K1000_TLS_DIR/ca.key is the CA's private key: it must not be on the box (not copied; delete it)"
fi

# --- 1. user and groups ------------------------------------------------------------------------
# The services run as k1000: i2c (MCP23017), gpio (interrupt lines), video (/dev/fb0), audio (ALSA).
if ! id k1000 >/dev/null 2>&1; then
  useradd --system --home-dir /var/lib/k1000 --shell /usr/sbin/nologin k1000
  changed "created system user k1000"
fi
for g in audio i2c gpio video; do
  getent group "$g" >/dev/null || { groupadd --system "$g"; changed "created group $g"; }
  if [[ " $(id -nG k1000) " != *" $g "* ]]; then usermod -aG "$g" k1000; changed "added k1000 to group $g"; fi
done

# --- 2. dependencies (apt only, and only what is missing) ----------------------------------------
missing=()
for p in "${APT_DEPS[@]}"; do dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p"); done
if [ ${#missing[@]} -gt 0 ]; then
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}"
  changed "apt installed ${missing[*]}"
fi
if [ -x /usr/local/bin/camilladsp ]; then
  cdsp_version=$(/usr/local/bin/camilladsp --version 2>&1 || true)
  [[ $cdsp_version == *" 5."* ]] || note "WARNING: /usr/local/bin/camilladsp is not v5 ($cdsp_version); the v0 configs need 5.x"
else
  note "WARNING: /usr/local/bin/camilladsp is missing; camilladsp.service will fail until it is installed"
fi

# --- 3. the application: box/ mirrored to /opt/k1000 ---------------------------------------------
# The whole box/ tree goes (package, tests, tools, units, README, this script), root-owned and
# read-only for k1000. Hidden files, caches and REVISION are never mirrored nor deleted; directory
# mtimes are not mirrored (-O), so writing REVISION does not make the next run see a change.
# apk/ neither: step 5b installs one APK from it, and keeps the box's when the source has none.
# Before anything changes, the running version is kept in /opt/k1000.prev (rollback: README).
if [ "$SRC" != "$APP" ]; then
  RSYNC=(rsync -rlptgoO --delete --chown=root:root "--chmod=D755,Fu+rw,Fgo+r,Fgo-w"
         --exclude='.*' --exclude=__pycache__ --exclude='*.pyc' --exclude=/REVISION --exclude=/apk)
  pending=$("${RSYNC[@]}" --dry-run --itemize-changes "$SRC/" "$APP/" 2>/dev/null || true)
  if [ -n "$pending" ]; then
    if [ -d "$APP/k1000" ]; then
      rsync -a --delete "$APP/" "$PREV/"
      note "previous version saved to $PREV"
    fi
    mkdir -p "$APP"
    n=$("${RSYNC[@]}" --itemize-changes "$SRC/" "$APP/" | wc -l)
    changed "synced $SRC -> $APP ($n entries)"
  fi
  rev=${K1000_REVISION:-$(revision_of "$SRC")}
  # Nothing synced and nothing better to record (no K1000_REVISION, no git, no REVISION file in an
  # rsync'd /opt/k1000-src): the REVISION already there still names what is deployed; keep it
  # rather than turn it into "unknown".
  if [ -z "${K1000_REVISION:-}" ] && [ -z "$pending" ] && [[ $rev == "unknown "* ]] && [ -s "$APP/REVISION" ]; then
    :
  else
    printf '%s\n' "$rev" > "$TMP/REVISION"
    put "$TMP/REVISION" "$APP/REVISION" 644
  fi
fi

# --- 4. settings, state and log directories ------------------------------------------------------
# /etc/k1000/k1000.yaml is Thomas's (profile pin, knob mapping): copied once, never overwritten.
if [ ! -e /etc/k1000/k1000.yaml ]; then
  if [ -f "$SRC/etc/k1000.yaml" ]; then
    install -D -m 0644 -o root -g root "$SRC/etc/k1000.yaml" /etc/k1000/k1000.yaml
    changed "installed /etc/k1000/k1000.yaml (first install)"
  else
    note "WARNING: $SRC/etc/k1000.yaml not found; k1000 will run on built-in defaults"
  fi
fi
own_dir /var/lib/k1000
own_dir /var/log/k1000
# Earlier versions gave CamillaGUI /var/lib/k1000/coeffs; it now has its own read-only folder (5.).
if [ -d /var/lib/k1000/coeffs ] && rmdir /var/lib/k1000/coeffs 2>/dev/null; then
  changed "removed the unused empty /var/lib/k1000/coeffs"
fi

# --- 4b. the phone page's certificate (K1000_TLS_DIR) -------------------------------------------
# The leaf certificate + key for https://10.42.0.1/ and the local CA's PUBLIC certificate (served to
# phones at http://10.42.0.1/k1000-ca.crt). k1000 reads them, nobody else: root:k1000 0640, in a
# root:k1000 0750 directory. The CA's private key is never copied, even when it sits in that dir.
# Checked in step 0.
TLS=/etc/k1000/tls
TLS_CHANGED=0
put_tls() {
  local src=$1 dst=$2
  if [ -f "$dst" ] && cmp -s "$src" "$dst" && [ "$(stat -c '%U:%G %a' "$dst")" = "root:k1000 640" ]; then
    return 0
  fi
  install -m 0640 -o root -g k1000 "$src" "$dst"
  changed "installed $dst (root:k1000 0640)"
  TLS_CHANGED=1
}
if [ -n "${K1000_TLS_DIR:-}" ]; then
  if [ "$(stat -c '%U:%G %a' "$TLS" 2>/dev/null)" != "root:k1000 750" ]; then
    install -d -o root -g k1000 -m 0750 "$TLS"
    changed "directory $TLS (root:k1000 750)"
  fi
  for f in "${TLS_FILES[@]}"; do put_tls "$K1000_TLS_DIR/$f" "$TLS/$f"; done
  if [ $TLS_CHANGED = 1 ] && [ $DO_START = 0 ] && systemctl is-active --quiet k1000.service; then
    note "new phone certificate: k1000 loads it at its next start (between shows: sudo systemctl restart k1000)"
  fi
elif [ ! -f "$TLS/k1000.crt" ]; then
  note "no phone certificate in $TLS: the phone page runs on plain HTTP (http://10.42.0.1:8080/, not installable). Deploy one: sudo K1000_TLS_DIR=<dir> $0"
fi

# --- 5. CamillaGUI -------------------------------------------------------------------------------
# A convenience surface: if it cannot be installed (offline, bad hash) the rest of the install
# still happens, and the script exits 3 at the end so the failure is not missed.
GUI_FAILED=0
install_gui() {
  local tgz=${CAMILLAGUI_TARBALL:-$TMP/camillagui.tar.gz}
  if [ -z "${CAMILLAGUI_TARBALL:-}" ]; then
    note "downloading CamillaGUI v5.0.0-beta.2"
    curl -fsSL --retry 3 -o "$tgz" "$GUI_URL" || { note "WARNING: CamillaGUI download failed (offline? set CAMILLAGUI_TARBALL=<tarball>)"; return 1; }
  fi
  echo "$GUI_TGZ_SHA256  $tgz" | sha256sum -c --quiet - || { note "WARNING: CamillaGUI tarball hash mismatch: $tgz"; return 1; }
  mkdir -p "$TMP/gui"
  tar xzf "$tgz" -C "$TMP/gui" ./camillagui || { note "WARNING: cannot unpack $tgz"; return 1; }
  echo "$GUI_BIN_SHA256  $TMP/gui/camillagui" | sha256sum -c --quiet - || { note "WARNING: CamillaGUI binary hash mismatch"; return 1; }
  install -d -m 0755 "$GUI" "$GUI/config" || return 1
  install -m 0755 -o root -g root "$TMP/gui/camillagui" "$GUI/camillagui" || return 1
  changed "installed $GUI/camillagui (v5.0.0-beta.2)"
}
if [ "$(sha256sum "$GUI/camillagui" 2>/dev/null | cut -d' ' -f1)" != "$GUI_BIN_SHA256" ]; then
  install_gui || GUI_FAILED=1
fi
# The GUI's config_dir/coeff_dir: empty, root-owned, and read-only inside its sandbox, so its
# unauthenticated file API has nothing to list, delete or overwrite (camillagui.yml says why).
own_dir "$GUI" root
own_dir "$GUI/files" root
own_dir "$GUI/files/coeffs" root
GUI_CHANGED=0
for f in camillagui.yml gui-config.yml; do
  put "$SRC/camillagui/$f" "$GUI/config/$f" 644
  if [ $PUT_CHANGED = 1 ]; then GUI_CHANGED=1; fi
done

# --- 5b. the Android app the first-time page offers (docs/06-phone.md §4) ----------------------
# The deploy puts the signed release APK in $SRC/apk/ (box/apk/ is gitignored: an APK is never in
# the repository). The newest K1000-<version>.apk there (sort -V) goes to /opt/k1000/apk/,
# root-owned 0644, and anything else in that folder is removed: the box offers one version, the
# one it was deployed with (a rollback copy of /opt/k1000.prev carries its own). No APK in the
# source: the box keeps the one it has. A file that is not a zip archive, or is over 64 MB (k1000's
# own limit: it holds the APK in memory), is not installed. k1000 loads it at its next start.
APK_DIR=$APP/apk
APK_RE='^K1000-[0-9][0-9A-Za-z.-]{0,31}\.apk$'
APK_NEW=0
apks=()
if [ -d "$SRC/apk" ]; then
  for f in "$SRC"/apk/*.apk; do
    [ -f "$f" ] || continue
    if [[ ${f##*/} =~ $APK_RE ]]; then apks+=("${f##*/}"); else note "WARNING: $f ignored (not K1000-<version>.apk)"; fi
  done
fi
apk=""
if [ ${#apks[@]} -gt 0 ]; then
  apk=$(printf '%s\n' "${apks[@]}" | sort -V | tail -n 1)
  if [ "$(head -c 4 "$SRC/apk/$apk" | od -An -tx1 | tr -d ' \n')" != 504b0304 ] \
     || [ "$(wc -c < "$SRC/apk/$apk")" -gt 67108864 ]; then
    note "WARNING: $SRC/apk/$apk is not an APK (a zip archive of at most 64 MB): not installed"
    apk=""
  fi
fi
if [ -n "$apk" ]; then
  own_dir "$APK_DIR" root
  put "$SRC/apk/$apk" "$APK_DIR/$apk" 644
  APK_NEW=$PUT_CHANGED
  for f in "$APK_DIR"/*; do
    if [ ! -e "$f" ] || [ "${f##*/}" = "$apk" ]; then continue; fi
    rm -rf -- "$f"
    changed "removed $f (the box offers $apk only)"
    APK_NEW=1
  done
else
  kept=""
  for f in "$APK_DIR"/K1000-*.apk; do
    if [ -f "$f" ]; then kept="$kept ${f##*/}"; fi
  done
  if [ -n "$kept" ]; then
    note "no app in $SRC/apk: the first-time page keeps offering$kept"
  else
    note "no Android app in $SRC/apk: the first-time page offers none (the deploy puts K1000-<version>.apk there)"
  fi
fi
if [ $APK_NEW = 1 ] && [ $DO_START = 0 ] && systemctl is-active --quiet k1000.service; then
  note "new Android app: the first-time page offers it once k1000 has started again (between shows: sudo systemctl restart k1000)"
fi

# --- 6. systemd: units, hardware watchdog --------------------------------------------------------
RELOAD=0 LAPTOP_NEW=0
for u in "${UNITS[@]}"; do
  put "$SRC/systemd/$u.service" "/etc/systemd/system/$u.service" 644
  if [ $PUT_CHANGED = 1 ]; then RELOAD=1; fi
done
for u in "${LAPTOP_UNITS[@]}"; do
  put "$SRC/systemd/$u" "/etc/systemd/system/$u" 644
  if [ $PUT_CHANGED = 1 ]; then RELOAD=1; LAPTOP_NEW=1; fi
done
# The phone AP's units (net/, D-007) are installed, updated and applied by net/setup-ap.sh alone
# (--ap below), which decides which of them run (backend, --down) and restarts the AP when a unit
# changed. Copying a newer unit here would hide that change from it (nothing left to apply), so
# here a changed unit is only reported.
AP_UNITS_STALE=()
for u in "${AP_UNITS[@]}"; do
  [ -e "/etc/systemd/system/$u.service" ] || continue
  cmp -s "$SRC/net/$u.service" "/etc/systemd/system/$u.service" || AP_UNITS_STALE+=("$u")
done
# Reboot the box if PID 1 stops feeding the BCM2835 watchdog for 15 s (a hung box on a gig is
# worse than a reboot: it comes back muted and ramps in). Overrides Pi OS's 40-rpi-* (1 min).
cat > "$TMP/watchdog.conf" <<'EOF'
# K1000 (box/install.sh): reboot if systemd (PID 1) stops responding for 15 s.
# Sorts after Raspberry Pi OS's 40-rpi-enable-watchdog.conf (1 min), so this value wins.
[Manager]
RuntimeWatchdogSec=15
EOF
put "$TMP/watchdog.conf" /etc/systemd/system.conf.d/50-k1000-watchdog.conf 644
if [ $PUT_CHANGED = 1 ]; then RELOAD=1; fi
# Keep the journal across reboots. Raspberry Pi OS keeps it in RAM only (40-rpi-volatile-
# storage.conf), so after a dropout and a power cycle k1000's log and the engine's start/stop
# lines were gone. Capped at 32 MB; synced to the SD card every 30 s (and at once for critical
# messages). Read the previous boot with `journalctl -b -1`.
cat > "$TMP/journald.conf" <<'EOF'
# K1000 (box/install.sh): keep the journal on disk across reboots, capped at 32 MB.
# Sorts after Raspberry Pi OS's 40-rpi-volatile-storage.conf (Storage=volatile), so this value wins.
[Journal]
Storage=persistent
SystemMaxUse=32M
SyncIntervalSec=30s
EOF
put "$TMP/journald.conf" /etc/systemd/journald.conf.d/50-k1000-journal.conf 644
if [ $PUT_CHANGED = 1 ]; then
  note "persistent journal: applies at the next reboot (or now: sudo systemctl restart systemd-journald)"
fi
if [ $RELOAD = 1 ]; then
  systemctl daemon-reload
  note "systemd reloaded"
fi
if [ $LAPTOP_NEW = 1 ] && [ $DO_START = 0 ] && systemctl is-active --quiet k1000.service \
   && ! systemctl is-active --quiet k1000-laptop.path; then
  note "SETUP > Laptop access works once k1000 has started again (it brings k1000-laptop.path; between shows: sudo systemctl restart k1000)"
fi
for u in "${UNITS[@]}"; do
  if ! systemctl is-enabled --quiet "$u.service" 2>/dev/null; then
    systemctl enable --quiet "$u.service"
    changed "enabled $u.service (starts at boot)"
  fi
done

# --- 7. optional: boot-level setup (--boot) ------------------------------------------------------
if [ $DO_BOOT = 1 ]; then
  [ -f "$BOOT_DIR/config.txt" ] && [ -f "$BOOT_DIR/cmdline.txt" ] || die "$BOOT_DIR has no config.txt/cmdline.txt"
  command -v dtc >/dev/null || { DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends device-tree-compiler; changed "apt installed device-tree-compiler"; }
  dtc -@ -q -I dts -O dtb -o "$TMP/k1000-zynscreen.dtbo" "$SRC/overlays/k1000-zynscreen.dts"
  put "$TMP/k1000-zynscreen.dtbo" "$BOOT_DIR/overlays/k1000-zynscreen.dtbo" -
  if ! grep -qE '^[[:space:]]*dtoverlay=k1000-zynscreen([,[:space:]]|$)' "$BOOT_DIR/config.txt"; then
    printf '\n[all]\n# K1000: ZynScreen 3.5" (ILI9486 + ADS7846 touch), box/overlays/k1000-zynscreen.dts\ndtoverlay=k1000-zynscreen,rotate=270\n' >> "$BOOT_DIR/config.txt"
    changed "config.txt: added dtoverlay=k1000-zynscreen,rotate=270"
  fi
  # The knob board (MCP23017 at 0x20) is on /dev/i2c-1: the I2C controller must be on and i2c-dev
  # loaded at boot (nothing loads it on demand). Raspberry Pi OS Lite does neither. The first box
  # only had them because config.txt was edited by hand and the desktop's sense-hat package
  # appends i2c-dev to /etc/modules. dtparam: only an [all]/[pi4] line counts (or none before the
  # first section).
  if ! awk 'BEGIN { s = "all" }
            /^[[:space:]]*\[/ { s = $0; gsub(/[][[:space:]]/, "", s); next }
            (s == "all" || s == "pi4") && /^[[:space:]]*dtparam=([^#]*,)?i2c(_arm)?=on([,[:space:]]|$)/ { f = 1 }
            END { exit !f }' "$BOOT_DIR/config.txt"; then
    printf '\n[all]\n# K1000: I2C for the knob board (MCP23017 at 0x20 on i2c-1)\ndtparam=i2c_arm=on\n' >> "$BOOT_DIR/config.txt"
    changed "config.txt: added dtparam=i2c_arm=on (knob board)"
  fi
  printf '# K1000 (box/install.sh --boot): /dev/i2c-1 for the knob board (MCP23017 at 0x20).\ni2c-dev\n' > "$TMP/k1000-modules.conf"
  put "$TMP/k1000-modules.conf" /etc/modules-load.d/k1000.conf 644
  if [ ! -d /sys/module/i2c_dev ]; then
    if modprobe i2c-dev; then changed "loaded i2c-dev now (it was not loaded)"; else note "WARNING: modprobe i2c-dev failed"; fi
  fi
  # No blinking console cursor on the ZynScreen between boot and the UI taking over.
  if ! grep -qE '(^|[[:space:]])vt\.global_cursor_default=0([[:space:]]|$)' "$BOOT_DIR/cmdline.txt"; then
    [ -e "$BOOT_DIR/cmdline.txt.k1000-orig" ] || cp -p "$BOOT_DIR/cmdline.txt" "$BOOT_DIR/cmdline.txt.k1000-orig"
    sed -i -E '1{s/[[:space:]]*vt\.global_cursor_default=[^[:space:]]*//g; s/[[:space:]]*$/ vt.global_cursor_default=0/}' "$BOOT_DIR/cmdline.txt"
    changed "cmdline.txt: added vt.global_cursor_default=0 (original kept as cmdline.txt.k1000-orig)"
  fi
  note "boot changes take effect at the next reboot"
fi

# --- 8. optional: headless appliance (--headless) ------------------------------------------------
# The desktop image auto-logs k1000 into labwc, which starts PipeWire + WirePlumber; they open ALSA
# cards (and race CamillaDSP for the M4 when it is plugged in). A DSP box needs neither.
if [ $DO_HEADLESS = 1 ]; then
  if [ "$(systemctl get-default)" != multi-user.target ]; then
    systemctl set-default multi-user.target
    changed "default target: multi-user.target (no desktop; undo: systemctl set-default graphical.target)"
  fi
  for u in pipewire.socket pipewire.service pipewire-pulse.socket pipewire-pulse.service wireplumber.service filter-chain.service; do
    if [ "$(systemctl --global is-enabled "$u" 2>/dev/null || true)" != masked ]; then
      systemctl --global mask --quiet "$u"
      changed "masked user unit $u for all users (undo: systemctl --global unmask $u)"
    fi
  done
  # The desktop image's services a DSP box never uses: Bluetooth, printing (cups watches for
  # printers), NFS client + rpcbind (port 111 open on every interface). Only units that are
  # plainly "enabled" are touched (a static unit cannot be disabled; absent ones are skipped).
  for u in bluetooth.service cups.service cups.socket cups.path rpcbind.service rpcbind.socket nfs-blkmap.service; do
    if [ "$(systemctl is-enabled "$u" 2>/dev/null || true)" = enabled ]; then
      systemctl disable --quiet "$u"
      changed "disabled $u (undo: systemctl enable $u)"
    fi
  done
  # The image auto-logs k1000 (passwordless sudo) into tty1: an invisible root-capable shell for
  # any USB keyboard, since the ZynScreen does not show the console. A local login then asks for
  # k1000's password (SSH is unchanged). The drop-in is renamed, not deleted (systemd reads only *.conf).
  autologin=/etc/systemd/system/getty@tty1.service.d/autologin.conf
  if [ -e "$autologin" ]; then
    mv "$autologin" "$autologin.k1000-off"
    changed "tty1 auto-login removed (undo: mv $autologin.k1000-off $autologin)"
  fi
  # Not done here: cloud-init. Turning it off starts k1000 ~2.5 s earlier, before the ZynScreen
  # registers (~7 s), so k1000's ExecStartPre would unbind the console before it exists.
  # k1000-console.service (installed above) closes that race; tools/k1000-lean.sh then turns
  # cloud-init off, and only once that unit is enabled.
  note "headless changes take effect at the next reboot"
fi

# --- 8b. optional: the phone's Wi-Fi access point (--ap) -----------------------------------------
# net/setup-ap.sh is idempotent, prints its own changes and keeps its earlier choices (band,
# portal, backend, interface, SSH: /etc/k1000/ap.info). The first time it takes its defaults
# (D-007 as refined: hostapd with PMF off and wlan0 unmanaged by NetworkManager, 5 GHz ch 36,
# country FR, portal off, SSH fenced off). It touches only the AP (k1000-ap-* units, or the
# k1000-ap NetworkManager connection), its DNS file, its fence and guard, /etc/k1000/ap.*, the
# Wi-Fi country and radio; never eth0. A Friday no-go (`net/setup-ap.sh --down`) stays down: --ap
# then only keeps the files current; `net/setup-ap.sh --up` brings the AP back.
# Its changes count in this script's total (its last "k1000-ap: N changes" line; pipefail keeps
# its exit status), so an update that only touches net/ is not "0 changes".
AP_FAILED=0
if [ $DO_AP = 1 ]; then
  if bash "$SRC/net/setup-ap.sh" 2>&1 | tee "$TMP/ap.log"; then :; else
    note "ERROR: net/setup-ap.sh failed (see above); everything else is installed"
    AP_FAILED=1
  fi
  n=$(sed -n 's/^k1000-ap: \([0-9][0-9]*\) changes$/\1/p' "$TMP/ap.log" | tail -n 1)
  CHANGES=$((CHANGES + ${n:-0}))
elif [ ${#AP_UNITS_STALE[@]} -gt 0 ]; then
  note "the phone AP's units changed (${AP_UNITS_STALE[*]}): not applied; between shows: sudo $0 --ap"
fi

# --- 9. optional: (re)start (--start) ------------------------------------------------------------
if [ $DO_START = 1 ]; then
  note "restarting ${UNITS[*]} (engine boots muted, k1000 ramps it in)"
  systemctl restart "${UNITS[@]/%/.service}"
elif [ $GUI_CHANGED = 1 ] && systemctl is-active --quiet camillagui.service; then
  note "camillagui config changed: 'sudo systemctl restart camillagui' to apply camillagui.yml (gui-config.yml applies on page reload)"
fi

for u in "${UNITS[@]}"; do
  note "$(printf '%-13s enabled=%-8s active=%s' "$u" "$(systemctl is-enabled "$u.service" 2>/dev/null || true)" "$(systemctl is-active "$u.service" 2>/dev/null || true)")"
done
for u in "${AP_UNITS[@]}"; do
  [ -e "/etc/systemd/system/$u.service" ] || continue
  note "$(printf '%-16s enabled=%-8s active=%s' "$u" "$(systemctl is-enabled "$u.service" 2>/dev/null || true)" "$(systemctl is-active "$u.service" 2>/dev/null || true)")"
done
note "deployed $(cat "$APP/REVISION" 2>/dev/null || echo '?'): $CHANGES changes"
# Exit 3: CamillaGUI failed; 4: the access point failed; 5: both. Everything else is installed.
if [ $GUI_FAILED = 1 ]; then
  note "ERROR: CamillaGUI is NOT installed (see the warning above); everything else is"
fi
if [ $AP_FAILED = 1 ]; then
  note "ERROR: the phone's access point is NOT set up (net/setup-ap.sh, see above); the knobs carry the show"
fi
if [ $GUI_FAILED = 1 ] && [ $AP_FAILED = 1 ]; then exit 5; fi
if [ $GUI_FAILED = 1 ]; then exit 3; fi
if [ $AP_FAILED = 1 ]; then exit 4; fi
