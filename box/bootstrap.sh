#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# K1000 bootstrap: a fresh Raspberry Pi OS Lite (64-bit, trixie) card -> a complete K1000 box
# (DECISIONS.md D-006). Run ON the box, as root, from a copy of the repo's box/ directory; the
# laptop-side commands are in README.md, "Fresh card (bootstrap)".
#
#   sudo /opt/k1000-src/bootstrap.sh            set everything up, then say to power-cycle
#   sudo /opt/k1000-src/bootstrap.sh --dry-run  say what it would do, change nothing
#   sudo /opt/k1000-src/bootstrap.sh --no-ap    everything but the phone's access point, which is
#                                               marked down (net/setup-ap.sh --down): a spare card
#                                               after a phone no-go. Later runs, with or without
#                                               --no-ap, keep it down; setup-ap.sh --up lifts it
#   sudo /opt/k1000-src/bootstrap.sh --old-engine  the old engine instead of the pinned build:
#                                               CamillaDSP 5.0.0-beta.1 from its release, the
#                                               fallback that played the gig of 18 Oct. It does NOT
#                                               meet D-018's 10 ms (step 2)
#
# The card must already have the k1000 user with its SSH key (Raspberry Pi Imager settings), and
# the box needs internet (apt, GitHub). Steps, each one skipped when already done:
#   1. apt-get update, only when a package K1000 needs is missing (TOOLS below, install.sh --deps),
#      then the operator tools listed in TOOLS (install.sh installs its own APT_DEPS in step 3)
#   2. The engine -> /usr/local/bin/camilladsp: the pinned build of D-019, CamillaDSP from upstream
#      `next5` at 0930f9b, made by tools/engine-build/build.sh and checked against its pinned
#      sha256 (the version string alone cannot tell builds apart). No release carries it, so there
#      is no download yet: give the binary with CAMILLADSP_BINARY. A box that runs it keeps it. A
#      box with another engine or none, and no binary given: the run stops before it changes
#      anything, and says how to build and give it. --old-engine installs 5.0.0-beta.1 instead
#      (its release tarball and binary checked against their own sums): never under 12.5 ms in to
#      out, and 13 to 15 ms more after one dropout (docs/17)
#   3. install.sh --boot --headless --ap: the K1000 units, the ZynScreen overlay, I2C for the knobs,
#      CamillaGUI (pinned by hash in install.sh), boot to the console, the phone's access point and
#      its fence (D-007, net/setup-ap.sh; the phone certificate is a separate step: README). A no-go
#      `setup-ap.sh --down` stays down; with --no-ap, no --ap and the AP is marked down
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
# K1000_BOOTSTRAP_FOREGROUND=1 runs in place, k1000-lean.sh included (it never detaches when
# bootstrap.sh runs it).
#
# CAMILLADSP_BINARY=<path> installs the engine from the binary tools/engine-build/build.sh made
# (offline too; not read on a box that already runs the pinned build);
# CAMILLADSP_TARBALL=<path> gives the old engine's release tarball (offline; --old-engine only);
# CAMILLAGUI_TARBALL=<path> does the same for CamillaGUI (passed on to install.sh);
# K1000_REVISION=<text> is passed on to install.sh (what /opt/k1000/REVISION records).
set -euo pipefail

SRC=$(cd "$(dirname "$0")" && pwd -P)
# The engine (D-019): upstream CamillaDSP, branch next5, at this commit, built for the box by
# tools/engine-build/build.sh (its PIN and PIN_SHA256 are these two). It reports
# "CamillaDSP 5.0.0-beta.2 (0930f9b)". The sha256 is the binary's: the build that was measured.
CDSP_PIN=0930f9bbfefa34a7b47c47497575f231dbe22b48
CDSP_PIN_SHA256=dd4106c927a5de2b31776cc28d797a3f42fb9b7d02deb928f45a6feee057d43c
# >>> HOSTING OF THE PINNED BUILD: not decided yet (the owner's call). Once it is, the URL of the
# binary itself (not an archive) goes here; it is checked against CDSP_PIN_SHA256 above, like a
# CAMILLADSP_BINARY file. Empty: no download, a fresh card needs CAMILLADSP_BINARY.
CDSP_PIN_URL=
# The old engine, with --old-engine only: CamillaDSP v5.0.0-beta.1, linux aarch64 (D-004), the
# engine of the gig of 18 Oct. Its binary reports "CamillaDSP 5.0.0-beta.1 (48bcd28)".
CDSP_OLD_URL=https://github.com/HEnquist/camilladsp/releases/download/v5.0.0-beta.1/camilladsp-linux-aarch64.tar.gz
CDSP_OLD_TGZ_SHA256=ebecf0c1cfaff12cd93bee033f45b71784f9f7efec34814b9f72fbb29876b493
CDSP_OLD_BIN_SHA256=d74325130dc323a67468523704fa8e815c6de7ca656788b7c3a36a3d51391ff5
CDSP_OLD_LATE="it does NOT meet D-018's 10 ms in to out: never under 12.5 ms, and 13 to 15 ms more after one dropout (docs/17)"
CDSP=${K1000_BOOTSTRAP_CDSP:-/usr/local/bin/camilladsp}     # override: tests only
# Operator tools that Raspberry Pi OS Lite lacks: i2cdetect for the knob board (I2C 0x20).
TOOLS=(i2c-tools)
SSHD_KEYONLY=/etc/ssh/sshd_config.d/10-k1000-keyonly.conf
LOGDIR=${K1000_BOOTSTRAP_LOGDIR:-/var/log/k1000-bootstrap}     # override: tests only
ARGS=("$@")

DRY=0 NO_AP=0 OLD_ENGINE=0
for arg in "$@"; do
  case $arg in
    --dry-run) DRY=1 ;;
    --no-ap) NO_AP=1 ;;
    --old-engine) OLD_ENGINE=1 ;;
    -h|--help) awk 'NR > 2 && /^#/ { sub(/^# ?/, ""); print; next } NR > 2 { exit }' "$0"; exit 0 ;;
    *) echo "bootstrap.sh: unknown option '$arg' (try --help)" >&2; exit 2 ;;
  esac
done

CHANGES=0 GUI_FAILED=0 AP_FAILED=0 TMP=
# The detached run (below) leaves its exit status for the terminal that follows it, whatever stops
# it: the checks and the engine decision before step 1 included.
trap 'rc=$?; if [ -n "$TMP" ]; then rm -rf "$TMP"; fi; if [ -n "${K1000_BOOTSTRAP_RC:-}" ]; then echo "$rc" > "$K1000_BOOTSTRAP_RC"; fi' EXIT
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

# --- the engine: what step 2 does, decided before anything changes --------------------------------
# A run that has no engine to install stops here, before apt and before it detaches: the same words
# in a dry run and in a real one, and nothing changed. Never the old engine without --old-engine.
sha256_of() { sha256sum "$1" 2>/dev/null | cut -d' ' -f1 || true; }
engine_is() {     # what $CDSP holds, in words, from its sha256 ($1)
  if [ -z "$1" ]; then echo "no engine"
  elif [ "$1" = "$CDSP_PIN_SHA256" ]; then echo "the pinned build (D-019)"
  elif [ "$1" = "$CDSP_OLD_BIN_SHA256" ]; then echo "the old engine, 5.0.0-beta.1"
  else echo "another build ($("$CDSP" --version 2>&1 | head -n 1), sha256 ${1:0:12}...)"
  fi
}
for e in CAMILLADSP_BINARY CAMILLADSP_TARBALL; do     # the detached run below starts in /
  if [ -n "${!e:-}" ]; then printf -v "$e" %s "$(realpath -m -- "${!e}")"; fi
done
have=$(sha256_of "$CDSP")
have_is=$(engine_is "$have")
ENGINE=keep ENGINE_FROM=
if [ $OLD_ENGINE = 1 ]; then
  [ -z "${CAMILLADSP_BINARY:-}" ] || die "--old-engine and CAMILLADSP_BINARY name two different engines: give one. Nothing was changed"
  if [ "$have" != "$CDSP_OLD_BIN_SHA256" ]; then
    ENGINE=old ENGINE_FROM=${CAMILLADSP_TARBALL:-$CDSP_OLD_URL}
    [ -z "${CAMILLADSP_TARBALL:-}" ] || [ "$(sha256_of "$CAMILLADSP_TARBALL")" = "$CDSP_OLD_TGZ_SHA256" ] \
      || die "CAMILLADSP_TARBALL=$CAMILLADSP_TARBALL is not the 5.0.0-beta.1 release tarball (missing, or another sha256). Nothing was changed"
  fi
elif [ "$have" = "$CDSP_PIN_SHA256" ]; then
  :     # kept; a CAMILLADSP_BINARY is not even read (a re-run after a power cycle may have lost it)
elif [ -n "${CAMILLADSP_BINARY:-}" ]; then
  sum=$(sha256_of "$CAMILLADSP_BINARY")
  [ -n "$sum" ] || die "CAMILLADSP_BINARY=$CAMILLADSP_BINARY: no such file. Nothing was changed"
  [ "$sum" = "$CDSP_PIN_SHA256" ] || die "CAMILLADSP_BINARY=$CAMILLADSP_BINARY is not the pinned build (sha256 $sum, expected $CDSP_PIN_SHA256). Nothing was changed. tools/engine-build/build.sh ends with \"this is the binary measured on the box\" when it made the pinned one"
  ENGINE=pin ENGINE_FROM=$CAMILLADSP_BINARY
elif [ -n "$CDSP_PIN_URL" ]; then
  ENGINE=pin ENGINE_FROM=$CDSP_PIN_URL
else
  {
    note "ERROR: no engine to install: this box has $have_is at $CDSP, and no CAMILLADSP_BINARY was given. Nothing was changed."
    note "K1000 runs the pinned build of D-019, CamillaDSP from upstream next5 at ${CDSP_PIN:0:7}. No release carries it, so there is no download yet. Build it on a machine with Docker, from the repository, copy it to the box and give it:"
    note "  box/tools/engine-build/build.sh     (its last line must be: this is the binary measured on the box)"
    note "  scp out/camilladsp k1000:/tmp/camilladsp"
    note "  sudo CAMILLADSP_BINARY=/tmp/camilladsp $SRC/$(basename "$0")${ARGS[*]:+ ${ARGS[*]}}     (on the box; K1000_REVISION as before)"
    note "Or the old engine, CamillaDSP 5.0.0-beta.1 from its release, the fallback that played the gig of 18 Oct: add --old-engine: $CDSP_OLD_LATE."
    if [ -n "${CAMILLADSP_TARBALL:-}" ]; then note "CAMILLADSP_TARBALL is the old engine's tarball: it is used with --old-engine only."; fi
  } >&2
  exit 1
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
  # K1000_BOOTSTRAP_CDSP too: the unit decides on the engine the terminal decided on
  for e in CAMILLADSP_BINARY CAMILLADSP_TARBALL CAMILLAGUI_TARBALL K1000_REVISION K1000_BOOTSTRAP_CDSP; do
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
TMP=$(mktemp -d)     # removed by the EXIT trap (top)

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
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing_tools[@]}"
    CHANGES=$((CHANGES + 1)); note "apt installed ${missing_tools[*]}"
  fi
fi

# --- 2. the engine --------------------------------------------------------------------------------
# What to do was decided above, before anything changed.
if [ $ENGINE = keep ]; then
  note "engine: $("$CDSP" --version 2>&1 | head -n 1), $have_is: nothing to do"
else
  what="the pinned build (D-019, next5 ${CDSP_PIN:0:7})"
  [ $ENGINE = pin ] || what="the OLD engine, 5.0.0-beta.1 (--old-engine)"
  if [ $DRY = 1 ]; then
    how="sha256 checked"
    case $ENGINE_FROM in https://*|http://*) how="sha256 checked after the download" ;; esac
    would "install CamillaDSP, $what, into $CDSP from $ENGINE_FROM ($how${have:+; it replaces $have_is})"
  else
    bin=$TMP/camilladsp want=$CDSP_PIN_SHA256
    if [ $ENGINE = old ]; then
      tgz=${CAMILLADSP_TARBALL:-$TMP/camilladsp.tar.gz} want=$CDSP_OLD_BIN_SHA256
      if [ -z "${CAMILLADSP_TARBALL:-}" ]; then
        note "downloading CamillaDSP 5.0.0-beta.1"
        curl -fsSL --retry 3 -o "$tgz" "$CDSP_OLD_URL" || die "CamillaDSP download failed (offline? set CAMILLADSP_TARBALL=<tarball>)"
      fi
      [ "$(sha256_of "$tgz")" = "$CDSP_OLD_TGZ_SHA256" ] || die "CamillaDSP tarball hash mismatch: $tgz"
      mkdir -p "$TMP/cdsp"
      tar xzf "$tgz" -C "$TMP/cdsp" camilladsp || die "cannot unpack camilladsp from $tgz"
      mv "$TMP/cdsp/camilladsp" "$bin"
    elif [ -n "${CAMILLADSP_BINARY:-}" ]; then
      cp "$CAMILLADSP_BINARY" "$bin"
    else
      note "downloading CamillaDSP, $what"
      curl -fsSL --retry 3 -o "$bin" "$CDSP_PIN_URL" || die "CamillaDSP download failed (offline? set CAMILLADSP_BINARY=<path>, built by tools/engine-build/build.sh)"
    fi
    [ "$(sha256_of "$bin")" = "$want" ] || die "CamillaDSP binary hash mismatch ($ENGINE_FROM): nothing installed"
    # A new file renamed into place: a running engine keeps its (old) binary until it restarts.
    install -m 0755 -o root -g root "$bin" "$CDSP.k1000-new"
    mv -f "$CDSP.k1000-new" "$CDSP"
    CHANGES=$((CHANGES + 1)); note "installed $CDSP: $("$CDSP" --version 2>&1 | head -n 1), $what${have:+ (it replaces $have_is)}"
  fi
fi
if [ $OLD_ENGINE = 1 ]; then note "WARNING: --old-engine, CamillaDSP 5.0.0-beta.1: $CDSP_OLD_LATE"; fi

# --- 3. K1000 itself ------------------------------------------------------------------------------
flags=(--boot --headless)
# The phone's access point (D-007): install.sh --ap (checked, so an older box/ copy still works),
# unless --no-ap. setup-ap.sh keeps a no-go (--down) down either way.
install_help=$("$SRC/install.sh" --help 2>/dev/null || true)
if [ $NO_AP = 0 ] && grep -Eq '(^|[[:space:]])--ap([[:space:]]|$)' <<<"$install_help"; then flags+=(--ap); fi
if [ $DRY = 1 ]; then
  # Not counted: install.sh has no dry run, so a finished card can still dry-run to 0 changes.
  note "would run: install.sh ${flags[*]} (units, overlay, CamillaGUI, headless boot; it has no dry run: its changes are not counted here)"
  if [ $NO_AP = 1 ]; then
    if grep -qx 'state=down' "${K1000_AP_ETC:-/etc/k1000}/ap.info" 2>/dev/null; then
      note "the phone's access point is already marked down"
    else
      would "mark the phone's access point down (net/setup-ap.sh --down)"
    fi
  fi
else
  rc=0
  "$SRC/install.sh" "${flags[@]}" 2>&1 | tee "$TMP/install.log" || rc=$?
  case $rc in
    0) ;;
    3) GUI_FAILED=1; note "WARNING: CamillaGUI is not installed (see above); going on: the engine, the knobs and the screen do not need it" ;;
    4) AP_FAILED=1; note "WARNING: the phone's access point is not set up (see above); going on: the knobs carry the show" ;;
    5) GUI_FAILED=1 AP_FAILED=1; note "WARNING: neither CamillaGUI nor the phone's access point is set up (see above); going on: the engine, the knobs and the screen do not need them" ;;
    *) die "install.sh ${flags[*]} failed (exit $rc, its output is above)" ;;
  esac
  # install.sh's own last line ("deployed ...: N changes") holds its total, setup-ap.sh's included
  n=$(sed -n 's/^k1000-install: .*: \([0-9][0-9]*\) changes$/\1/p' "$TMP/install.log" | tail -n 1)
  CHANGES=$((CHANGES + ${n:-0}))
  # --no-ap: the access point marked down, so no later install.sh --ap puts it on the air
  if [ $NO_AP = 1 ] && [ -x "$SRC/net/setup-ap.sh" ]; then
    if "$SRC/net/setup-ap.sh" --down 2>&1 | tee "$TMP/ap.log"; then
      n=$(sed -n 's/^k1000-ap: \([0-9][0-9]*\) changes$/\1/p' "$TMP/ap.log" | tail -n 1)
      CHANGES=$((CHANGES + ${n:-0}))
    else
      note "WARNING: could not mark the phone's access point down (see above): nothing was set up for it anyway"
    fi
  fi
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
# In place, never as its own unit: this run is already a unit, or deliberately in the foreground
# (K1000_BOOTSTRAP_FOREGROUND=1, maybe because systemd-run is missing, which lean would need).
lean=(env K1000_LEAN_FOREGROUND=1 "$SRC/tools/k1000-lean.sh")
[ $DRY = 0 ] || lean+=(--dry-run)
"${lean[@]}" 2>&1 | tee "$TMP/lean.log" || die "k1000-lean.sh failed (its output is above); nothing else to do until that is fixed"
n=$(sed -n 's/.*[: ]\([0-9][0-9]*\) changes.*$/\1/p' "$TMP/lean.log" | tail -n 1)
CHANGES=$((CHANGES + ${n:-0}))

# --- summary ----------------------------------------------------------------------------------------
# "already a K1000" only when nothing is left to do: a step that is still open is said instead.
todo=()
if [ $DRY = 0 ] && grep -q 'cloud-init left on' "$TMP/lean.log"; then todo+=("cloud-init off"); fi
[ $AP_FAILED = 0 ] || todo+=("the phone's access point")
[ $GUI_FAILED = 0 ] || todo+=("CamillaGUI")
if [ $DRY = 1 ]; then
  note "dry run: $CHANGES changes (install.sh's not included: it has no dry run)"
elif [ $CHANGES = 0 ] && [ ${#todo[@]} = 0 ]; then
  note "0 changes: this card is already a K1000"
elif [ $CHANGES = 0 ]; then
  note "0 changes; still to do: $(printf '%s, ' "${todo[@]}" | sed 's/, $//') (below)"
else
  note "$CHANGES changes. Now power-cycle the box (unplug, 5 s, plug back in): the boot-level changes"
  note "apply then. ~20 s later: ssh k1000 'systemctl is-active camilladsp k1000 camillagui' says active x3,"
  note "the panel shows K1000, and Main ramps in to the saved level. Profile: sudo nano /etc/k1000/k1000.yaml"
fi
if [ $DRY = 0 ] && grep -q 'cloud-init left on' "$TMP/lean.log"; then
  note "cloud-init is still on (see above): after the next power cycle, run bootstrap.sh once more to turn it off"
fi
if [ $OLD_ENGINE = 1 ]; then note "WARNING: --old-engine, CamillaDSP 5.0.0-beta.1: $CDSP_OLD_LATE"; fi
if [ $AP_FAILED = 1 ]; then
  note "ERROR: the phone's access point is NOT set up: sudo /opt/k1000/net/setup-ap.sh --status, then run this again"
fi
if [ $GUI_FAILED = 1 ]; then
  note "ERROR: CamillaGUI is NOT installed: run this again with internet, or CAMILLAGUI_TARBALL=<tarball>"
  exit 3
fi
if [ $AP_FAILED = 1 ]; then exit 4; fi
