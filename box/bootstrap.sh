#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# K1000 bootstrap: a fresh Raspberry Pi OS Lite (64-bit, trixie) card -> a complete K1000 box
# (DECISIONS.md D-006). Run ON the box, as root, from a copy of the repo's box/ directory; the
# laptop-side commands are in README.md, "Fresh card (bootstrap)".
#
#   sudo /opt/k1000-src/bootstrap.sh            set everything up, then say to power-cycle
#   sudo /opt/k1000-src/bootstrap.sh --dry-run  say what it would do, change nothing
#
# The card must already have the k1000 user with its SSH key (Raspberry Pi Imager settings), and
# the box needs internet (apt, GitHub). Steps, each one skipped when already done:
#   1. apt-get update, only when a package K1000 needs is missing (TOOLS below, install.sh --deps),
#      then the operator tools listed in TOOLS (install.sh installs its own APT_DEPS in step 3)
#   2. CamillaDSP 5.0.0-beta.1 -> /usr/local/bin/camilladsp: the release tarball and the binary are
#      both checked against pinned sha256 sums (the version string alone cannot tell builds apart)
#   3. install.sh --boot --headless (and --ap once install.sh offers it): the K1000 units, the
#      ZynScreen overlay, I2C for the knobs, CamillaGUI (pinned by hash in install.sh), boot to the
#      console
#   4. SSH: keys only (/etc/ssh/sshd_config.d/10-k1000-keyonly.conf, like the first box), only once
#      a public-key login of k1000 has been seen since boot; applies at the next boot
#   5. tools/k1000-lean.sh: the lean OS (no desktop, no apt timers, zram only, quiet boot). It
#      leaves cloud-init on until the panel guard (k1000-console.service) has run at a boot: run
#      bootstrap.sh once more after the first power cycle; that run turns cloud-init off.
# It never reboots and never runs apt upgrade. Power-cycle when it says so. On a finished card a
# re-run changes nothing and says "0 changes".
#
# A real run detaches from the terminal, like k1000-lean.sh: it re-runs itself as the transient
# systemd unit k1000-bootstrap-<time> and follows its log (/var/log/k1000-bootstrap/run-<time>.log,
# exit status in run-<time>.rc), so a dropped SSH session cannot kill apt halfway.
# K1000_BOOTSTRAP_FOREGROUND=1 runs in place.
#
# CAMILLADSP_TARBALL=<path> installs CamillaDSP from a local copy of the release tarball (offline);
# CAMILLAGUI_TARBALL=<path> does the same for CamillaGUI (passed on to install.sh);
# K1000_REVISION=<text> is passed on to install.sh (what /opt/k1000/REVISION records).
set -euo pipefail

SRC=$(cd "$(dirname "$0")" && pwd -P)
# CamillaDSP v5.0.0-beta.1, linux aarch64 (D-004): the binary on the first box came from this
# tarball (same sha256), and reports "CamillaDSP 5.0.0-beta.1 (48bcd28)".
CDSP_URL=https://github.com/HEnquist/camilladsp/releases/download/v5.0.0-beta.1/camilladsp-linux-aarch64.tar.gz
CDSP_TGZ_SHA256=ebecf0c1cfaff12cd93bee033f45b71784f9f7efec34814b9f72fbb29876b493
CDSP_BIN_SHA256=d74325130dc323a67468523704fa8e815c6de7ca656788b7c3a36a3d51391ff5
CDSP=/usr/local/bin/camilladsp
# Operator tools that Raspberry Pi OS Lite lacks: i2cdetect for the knob board (I2C 0x20).
TOOLS=(i2c-tools)
SSHD_KEYONLY=/etc/ssh/sshd_config.d/10-k1000-keyonly.conf
LOGDIR=${K1000_BOOTSTRAP_LOGDIR:-/var/log/k1000-bootstrap}     # override: tests only
ARGS=("$@")

DRY=0
for arg in "$@"; do
  case $arg in
    --dry-run) DRY=1 ;;
    -h|--help) awk 'NR > 2 && /^#/ { sub(/^# ?/, ""); print; next } NR > 2 { exit }' "$0"; exit 0 ;;
    *) echo "bootstrap.sh: unknown option '$arg' (try --help)" >&2; exit 2 ;;
  esac
done

CHANGES=0 GUI_FAILED=0
note() { printf 'k1000-bootstrap: %s\n' "$*"; }
die() { printf 'k1000-bootstrap: ERROR: %s\n' "$*" >&2; exit 1; }
would() { CHANGES=$((CHANGES + 1)); note "would: $*"; }

[ $DRY = 1 ] || [ "$(id -u)" = 0 ] || die "run as root: sudo $0"
[ -x "$SRC/install.sh" ] && [ -x "$SRC/tools/k1000-lean.sh" ] || die "$SRC does not look like box/ (no install.sh, tools/k1000-lean.sh)"
[ "$(dpkg --print-architecture 2>/dev/null)" = arm64 ] || die "not an arm64 Raspberry Pi OS (dpkg says '$(dpkg --print-architecture 2>/dev/null)')"
grep -q '^VERSION_CODENAME=trixie' /etc/os-release || note "WARNING: not trixie ($(grep '^VERSION_CODENAME=' /etc/os-release)): untested"
KEYS_OK=0
if id k1000 >/dev/null 2>&1 && [ -s ~k1000/.ssh/authorized_keys ]; then KEYS_OK=1; else
  note "WARNING: no k1000 login user with an SSH key: create it with Raspberry Pi Imager (install.sh would make a system user without a login)"
fi

# --- detach from the terminal ---------------------------------------------------------------------
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
if [ $DRY = 0 ] && [ -z "${INVOCATION_ID:-}" ] && [ "${K1000_BOOTSTRAP_FOREGROUND:-0}" != 1 ]; then
  command -v systemd-run >/dev/null || die "systemd-run not found (on a console on the box: K1000_BOOTSTRAP_FOREGROUND=1 $0)"
  ts=$(date +%Y%m%d-%H%M%S)
  unit=k1000-bootstrap-$ts log=$LOGDIR/run-$ts.log rcf=$LOGDIR/run-$ts.rc
  mkdir -p "$LOGDIR"
  : > "$log"
  envs=()
  for e in CAMILLADSP_TARBALL CAMILLAGUI_TARBALL K1000_REVISION; do
    if [ -n "${!e:-}" ]; then envs+=("--setenv=$e=${!e}"); fi
  done
  systemd-run --quiet --collect --unit="$unit" -p "StandardOutput=append:$log" -p "StandardError=append:$log" \
    "${envs[@]}" "--setenv=K1000_BOOTSTRAP_RC=$rcf" "$SRC/$(basename "$0")" "${ARGS[@]}" \
    || die "systemd-run failed: nothing was run"
  note "running as the systemd unit $unit: a lost SSH session no longer interrupts it"
  note "log: $log   running: systemctl is-active $unit   exit status (when done): $rcf"
  follow "$log" "$rcf" "$unit"
  [ -s "$rcf" ] || die "$unit ended without an exit status (killed?): read $log"
  exit "$(cat "$rcf")"
fi
TMP=$(mktemp -d)
# The detached run also leaves its exit status for the terminal that follows it (above).
trap 'rc=$?; rm -rf "$TMP"; if [ -n "${K1000_BOOTSTRAP_RC:-}" ]; then echo "$rc" > "$K1000_BOOTSTRAP_RC"; fi' EXIT

# --- 1. apt: package lists, operator tools --------------------------------------------------------
# apt-get update only when something has to be installed: a re-run on a finished card at a gig
# (no internet, or a LAN without DNS) must not wait on apt's timeouts.
missing=() need=()
mapfile -t need < <("$SRC/install.sh" --deps 2>/dev/null || true)
for p in "${TOOLS[@]}" "${need[@]}"; do dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p"); done
missing_tools=()
for p in "${TOOLS[@]}"; do [[ " ${missing[*]} " != *" $p "* ]] || missing_tools+=("$p"); done
if [ ${#missing[@]} = 0 ]; then
  note "apt: everything K1000 needs is installed (no apt-get update)"
elif [ $DRY = 1 ]; then
  note "would run: apt-get update (missing: ${missing[*]})"
  [ ${#missing_tools[@]} = 0 ] || would "apt-get install ${missing_tools[*]}"
else
  # install.sh installs its APT_DEPS from these package lists (step 3).
  apt-get update -q || note "WARNING: apt-get update failed (offline?): using the package lists already there"
  if [ ${#missing_tools[@]} -gt 0 ]; then
    apt-get install -y --no-install-recommends "${missing_tools[@]}"
    CHANGES=$((CHANGES + 1)); note "apt installed ${missing_tools[*]}"
  fi
fi

# --- 2. CamillaDSP --------------------------------------------------------------------------------
have=$(sha256sum "$CDSP" 2>/dev/null | cut -d' ' -f1 || true)
if [ "$have" = "$CDSP_BIN_SHA256" ]; then
  note "CamillaDSP: $("$CDSP" --version 2>&1 | head -n 1) (pinned build, nothing to do)"
elif [ $DRY = 1 ]; then
  would "install CamillaDSP 5.0.0-beta.1 into $CDSP (${have:+replacing a different build }from ${CAMILLADSP_TARBALL:-$CDSP_URL})"
else
  tgz=${CAMILLADSP_TARBALL:-$TMP/camilladsp.tar.gz}
  if [ -z "${CAMILLADSP_TARBALL:-}" ]; then
    note "downloading CamillaDSP 5.0.0-beta.1"
    curl -fsSL --retry 3 -o "$tgz" "$CDSP_URL" || die "CamillaDSP download failed (offline? set CAMILLADSP_TARBALL=<tarball>)"
  fi
  echo "$CDSP_TGZ_SHA256  $tgz" | sha256sum -c --quiet - || die "CamillaDSP tarball hash mismatch: $tgz"
  mkdir -p "$TMP/cdsp"
  tar xzf "$tgz" -C "$TMP/cdsp" camilladsp || die "cannot unpack camilladsp from $tgz"
  echo "$CDSP_BIN_SHA256  $TMP/cdsp/camilladsp" | sha256sum -c --quiet - || die "CamillaDSP binary hash mismatch"
  # A new file renamed into place: a running engine keeps its (old) binary until it restarts.
  install -m 0755 -o root -g root "$TMP/cdsp/camilladsp" "$CDSP.k1000-new"
  mv -f "$CDSP.k1000-new" "$CDSP"
  CHANGES=$((CHANGES + 1)); note "installed $CDSP: $("$CDSP" --version 2>&1 | head -n 1)"
fi

# --- 3. K1000 itself ------------------------------------------------------------------------------
flags=(--boot --headless)
# The phone feature (D-007) adds an --ap flag to install.sh; use it as soon as it exists.
install_help=$("$SRC/install.sh" --help 2>/dev/null || true)
if grep -Eq '(^|[[:space:]])--ap([[:space:]]|$)' <<<"$install_help"; then flags+=(--ap); fi
if [ $DRY = 1 ]; then
  would "run install.sh ${flags[*]} (units, overlay, CamillaGUI, headless boot; install.sh has no dry run)"
else
  rc=0
  "$SRC/install.sh" "${flags[@]}" 2>&1 | tee "$TMP/install.log" || rc=$?
  case $rc in
    0) ;;
    3) GUI_FAILED=1; note "WARNING: CamillaGUI is not installed (see above); going on: the engine, the knobs and the screen do not need it" ;;
    *) die "install.sh ${flags[*]} failed (exit $rc, its output is above)" ;;
  esac
  n=$(sed -n 's/.*: \([0-9][0-9]*\) changes$/\1/p' "$TMP/install.log" | tail -n 1)
  CHANGES=$((CHANGES + ${n:-0}))
fi

# --- 4. SSH: keys only ----------------------------------------------------------------------------
# The first box has this drop-in (made by hand): no password and no keyboard-interactive login,
# which matters once the box runs its own Wi-Fi access point for phones (D-007). Written only when
# a public-key login of k1000 has worked since boot (so nobody is locked out), checked with
# sshd -t, never rewritten once there, and sshd is not restarted: it applies at the next boot.
if [ -e "$SSHD_KEYONLY" ]; then
  :
elif [ $KEYS_OK = 0 ]; then
  note "WARNING: SSH still accepts passwords: k1000 has no ~/.ssh/authorized_keys"
elif ! journalctl -b -t sshd-session -t sshd -o cat --no-pager 2>/dev/null > "$TMP/sshd.log" \
     || ! grep -q '^Accepted publickey for k1000 ' "$TMP/sshd.log"; then
  note "WARNING: SSH left as it is (passwords may still work): no public-key login of k1000 seen since boot; log in with your key (ssh k1000 true), then run this again"
elif [ $DRY = 1 ]; then
  would "SSH: keys only ($SSHD_KEYONLY; applies at the next boot)"
else
  printf '# K1000 (box/bootstrap.sh): SSH logins by key only.\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n' > "$SSHD_KEYONLY.k1000-new"
  chmod 644 "$SSHD_KEYONLY.k1000-new"
  mv "$SSHD_KEYONLY.k1000-new" "$SSHD_KEYONLY"
  if ! sshd -t; then
    rm -f "$SSHD_KEYONLY"
    die "sshd -t refused the configuration with $SSHD_KEYONLY: removed it again; SSH is unchanged"
  fi
  CHANGES=$((CHANGES + 1)); note "SSH: keys only ($SSHD_KEYONLY; applies at the next boot; undo: rm $SSHD_KEYONLY)"
fi

# --- 5. the lean OS -------------------------------------------------------------------------------
lean=("$SRC/tools/k1000-lean.sh")
[ $DRY = 0 ] || lean+=(--dry-run)
"${lean[@]}" 2>&1 | tee "$TMP/lean.log" || die "k1000-lean.sh failed (its output is above); nothing else to do until that is fixed"
n=$(sed -n 's/.*[: ]\([0-9][0-9]*\) changes.*$/\1/p' "$TMP/lean.log" | tail -n 1)
CHANGES=$((CHANGES + ${n:-0}))

# --- summary ----------------------------------------------------------------------------------------
if [ $DRY = 1 ]; then
  note "dry run: $CHANGES changes"
elif [ $CHANGES = 0 ]; then
  note "0 changes: this card is already a K1000"
else
  note "$CHANGES changes. Now power-cycle the box (unplug, 5 s, plug back in): the boot-level changes"
  note "apply then. ~20 s later: ssh k1000 'systemctl is-active camilladsp k1000 camillagui' says active x3,"
  note "the panel shows K1000, and Main ramps in to the saved level. Profile: sudo nano /etc/k1000/k1000.yaml"
fi
if [ $DRY = 0 ] && grep -q 'cloud-init left on' "$TMP/lean.log"; then
  note "cloud-init is still on (see above): after the next power cycle, run bootstrap.sh once more to turn it off"
fi
if [ $GUI_FAILED = 1 ]; then
  note "ERROR: CamillaGUI is NOT installed: run this again with internet, or CAMILLAGUI_TARBALL=<tarball>"
  exit 3
fi
