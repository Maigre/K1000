#!/bin/bash
# K1000 installer: deploys box/ onto the Raspberry Pi. Idempotent; a re-run with nothing new
# changes nothing and ends with "0 changes". See README.md (same directory) for the full picture.
#
#   sudo ./install.sh             install/update files, enable the 4 units. Starts NOTHING.
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
#   ./install.sh --deps           print the apt packages it may install, one per line (bootstrap.sh
#                                 runs apt-get update only when one is missing)
#
# Run it from a checkout of box/ or an rsync'd copy (e.g. /opt/k1000-src), never from /opt/k1000.prev.
# K1000_REVISION=<text> records what is deployed in /opt/k1000/REVISION (default: git describe,
#   else the source tree's own REVISION file, e.g. a rollback copy of /opt/k1000.prev).
# K1000_BOOT_DIR overrides /boot/firmware (only to test --boot on a copy).
# CAMILLAGUI_TARBALL=<path> installs CamillaGUI from a local tarball instead of downloading it.
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
UNITS=(camilladsp k1000 camillagui k1000-console)
# smbus2 replaces smbus (D-008) and qrcode draws the phone's join codes (D-007); alsa-utils (aplay/
# arecord probe the cards), fonts-dejavu-core (the UI's fonts) and usbutils (lsusb, M4 bring-up)
# are not in Raspberry Pi OS Lite.
APT_DEPS=(python3-websockets python3-smbus python3-smbus2 python3-libgpiod python3-yaml python3-pil
          python3-numpy python3-qrcode python3-pytest rsync alsa-utils fonts-dejavu-core usbutils)
# CamillaGUI v5.0.0-beta.2, linux aarch64. Pinned by hash: its binary reports "5.0.0-beta.1" in
# --version, so the version string cannot tell an upgrade from a no-op.
GUI_URL=https://github.com/HEnquist/camillagui-backend/releases/download/v5.0.0-beta.2/camillagui_linux_aarch64.tar.gz
GUI_TGZ_SHA256=854552f3e74a156d25117a1ecca2df44c94aefbeb62e1c47ff23b19d6b0cc8a2
GUI_BIN_SHA256=d993616caf6c67dc09b11a337d484ca2eca6c21c517305dc9d95df2f2e7bf14e

DO_START=0 DO_BOOT=0 DO_HEADLESS=0
for arg in "$@"; do
  case $arg in
    --start) DO_START=1 ;;
    --boot) DO_BOOT=1 ;;
    --headless) DO_HEADLESS=1 ;;
    --deps) printf '%s\n' "${APT_DEPS[@]}" device-tree-compiler; exit 0 ;;   # dtc: --boot
    -h|--help) awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; exit 0 ;;
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
  apt-get install -y --no-install-recommends "${missing[@]}"
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
# Before anything changes, the running version is kept in /opt/k1000.prev (rollback: README).
if [ "$SRC" != "$APP" ]; then
  RSYNC=(rsync -rlptgoO --delete --chown=root:root "--chmod=D755,Fu+rw,Fgo+r,Fgo-w"
         --exclude='.*' --exclude=__pycache__ --exclude='*.pyc' --exclude=/REVISION)
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
  printf '%s\n' "$rev" > "$TMP/REVISION"
  put "$TMP/REVISION" "$APP/REVISION" 644
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

# --- 6. systemd: units, hardware watchdog --------------------------------------------------------
RELOAD=0
for u in "${UNITS[@]}"; do
  put "$SRC/systemd/$u.service" "/etc/systemd/system/$u.service" 644
  if [ $PUT_CHANGED = 1 ]; then RELOAD=1; fi
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
for u in "${UNITS[@]}"; do
  if ! systemctl is-enabled --quiet "$u.service" 2>/dev/null; then
    systemctl enable --quiet "$u.service"
    changed "enabled $u.service (starts at boot)"
  fi
done

# --- 7. optional: boot-level setup (--boot) ------------------------------------------------------
if [ $DO_BOOT = 1 ]; then
  [ -f "$BOOT_DIR/config.txt" ] && [ -f "$BOOT_DIR/cmdline.txt" ] || die "$BOOT_DIR has no config.txt/cmdline.txt"
  command -v dtc >/dev/null || { apt-get install -y --no-install-recommends device-tree-compiler; changed "apt installed device-tree-compiler"; }
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
note "deployed $(cat "$APP/REVISION" 2>/dev/null || echo '?'): $CHANGES changes"
if [ $GUI_FAILED = 1 ]; then
  note "ERROR: CamillaGUI is NOT installed (see the warning above); everything else is"
  exit 3
fi
