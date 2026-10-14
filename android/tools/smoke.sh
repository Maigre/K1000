#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Emulator smoke test of the K1000 app (docs/09-android.md, "Tests"). Needs Docker and /dev/kvm.
#
#   tools/smoke.sh [api] [pwa-static-dir]
#     api             33 (Android 13, like Thomas's Pixel 4a; default) or 37 (Android 17)
#     pwa-static-dir  the box's PWA files (default: ../box/k1000/web/static)
#   K1000_RELEASE_APK  the signed release APK to check too (default: the newest
#                      $K1000_SIGNING_DIR/out/K1000-*.apk, if any)
#   K1000_SMOKE_WIPE=1 cold-boot the emulator from a wiped data partition (an AVD left in a bad
#                      state by an emulator that hung)
#   K1000_PREVIOUS_APK the signed release APK before it (e.g. K1000-0.2.1.apk): the release APK
#                      must install over it as an update (same key, higher versionCode)
#   K1000_SMOKE_ONLY=boxwifi  only the checks of the K1000 Wi-Fi taken from the box (app 0.2.1)
#   K1000_SMOKE_ONLY=mic      only the phone-mic checks (app 0.2.2): the grant rule, Android's dialog,
#                             the microphone released when K1000 leaves the screen
#
# 1. makes a throwaway name-constrained test CA (+ leaves) in a temporary directory, keys included,
#    deleted at the end; never the K1000 CA;
# 2. builds the SMOKE APK with that CA (-Pk1000.smokeCa), in the build container;
# 3. boots an x86_64 Google APIs emulator (headless, KVM) in the emulator container, its
#    simulated access point turned into a WPA2 "K1000" (netsim), points 10.42.0.1:443 at
#    tools/smoke_server.py, and runs tools/smoke_driver.py.
# The emulator image and AVDs live in Docker volumes (k1000-android-sdk, k1000-android-avd).
# Emulator: the SDK's stable "emulator" package (37.2.12 on 2026-10-12), a test-only tool.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
API=${1:-33}
STATIC=$(cd "${2:-$here/../box/k1000/web/static}" && pwd)
case $API in
  33) SYSDIR="system-images/android-33/google_apis/x86_64" ;;
  37) SYSDIR="system-images/android-37.0/google_apis/x86_64" ;;
  *) echo "api: 33 or 37" >&2; exit 2 ;;
esac
[ -e /dev/kvm ] || { echo "no /dev/kvm: cannot run the emulator here" >&2; exit 1; }
[ -f "$STATIC/index.html" ] || { echo "no PWA in $STATIC" >&2; exit 1; }
work=$(mktemp -d "${TMPDIR:-/tmp}/k1000-smoke.XXXXXX")
name=k1000-smoke-$API
cleanup() {
  docker exec "$name" adb emu kill >/dev/null 2>&1 && sleep 5 || true
  docker rm -f "$name" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

# --- 1. throwaway certificates
NB=$(date -u -d '-1 day' +%Y%m%d%H%M%SZ); NA=$(date -u -d '+30 days' +%Y%m%d%H%M%SZ)
mkca() {  # name O
  openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$work/$1-ca.key" 2>/dev/null
  printf '[req]\ndistinguished_name=dn\nprompt=no\nx509_extensions=v3\n[dn]\nCN=K1000 Smoke CA (throwaway)\nO=%s\n[v3]\nbasicConstraints=critical,CA:TRUE,pathlen:0\nkeyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\nnameConstraints=critical,permitted;IP:10.42.0.0/255.255.255.0,permitted;DNS:k1000.home.arpa\n' "$2" > "$work/$1-ca.cnf"
  openssl req -new -x509 -key "$work/$1-ca.key" -config "$work/$1-ca.cnf" -not_before "$NB" -not_after "$NA" -out "$work/$1-ca.pem" 2>/dev/null
}
mkleaf() {  # ca leaf san
  openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$work/$2.key" 2>/dev/null
  openssl req -new -key "$work/$2.key" -subj /CN=k1000.home.arpa -out "$work/$2.csr" 2>/dev/null
  printf 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\nsubjectAltName=%s\n' "$3" > "$work/$2.ext"
  openssl x509 -req -in "$work/$2.csr" -CA "$work/$1-ca.pem" -CAkey "$work/$1-ca.key" -set_serial "0x$(openssl rand -hex 8)" \
    -not_before "$NB" -not_after "$NA" -extfile "$work/$2.ext" -out "$work/$2.pem" 2>/dev/null
}
mkca smoke "K1000 tests"
mkca look "K1000 tests"       # same name as the smoke CA, other key
mkleaf smoke good "IP:10.42.0.1,DNS:k1000.home.arpa"
mkleaf smoke otherip "IP:10.42.0.5"
mkleaf smoke extradns "IP:10.42.0.1,DNS:k1000.home.arpa,DNS:evil.example"
mkleaf look wrongca "IP:10.42.0.1,DNS:k1000.home.arpa"

# --- 2. the smoke APK with the throwaway CA (images: android/build.sh's, and the emulator one)
BUILD_IMAGE=k1000-android-build:2
EMU_IMAGE=k1000-android-emu:2
docker image inspect "$BUILD_IMAGE" >/dev/null 2>&1 || docker build --target build -t "$BUILD_IMAGE" "$here/docker"
docker image inspect "$EMU_IMAGE" >/dev/null 2>&1 || docker build --target emu -t "$EMU_IMAGE" "$here/docker"
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/gradle-home/home -e ANDROID_USER_HOME=/gradle-home/android-user \
  -v k1000-android-sdk:/opt/android-sdk -v k1000-android-gradle:/gradle-home -v "$here":/src -v "$work":/smoke:ro -w /src \
  "$BUILD_IMAGE" ./gradlew --no-daemon --console=plain -q assembleSmoke -Pk1000.smokeCa=/smoke/smoke-ca.pem
cp "$here/app/build/outputs/apk/smoke/app-smoke.apk" "$work/app-smoke.apk"
REL=${K1000_RELEASE_APK:-$(ls -t "${K1000_SIGNING_DIR:-$HOME/Bakery/37Projects/secrets/k1000-android}"/out/K1000-*.apk 2>/dev/null | head -n 1)}
REL_ARG=()
if [ -n "$REL" ] && [ -f "$REL" ]; then cp "$REL" "$work/release.apk"; REL_ARG=(--release-apk /smoke/release.apk); fi
if [ ${#REL_ARG[@]} -gt 0 ] && [ -n "${K1000_PREVIOUS_APK:-}" ] && [ -f "$K1000_PREVIOUS_APK" ]; then
  cp "$K1000_PREVIOUS_APK" "$work/previous.apk"; REL_ARG+=(--previous-apk /smoke/previous.apk)
fi

# --- 3. emulator + driver. The test-only SDK packages (adb, the emulator, the system image) come
# at sdkmanager's latest revision; they are printed, and never used by the build.
docker volume create k1000-android-avd >/dev/null
docker run --rm -v k1000-android-avd:/avd "$EMU_IMAGE" chown "$(id -u):$(id -g)" /avd
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/gradle-home/home -v k1000-android-sdk:/opt/android-sdk \
  -v k1000-android-gradle:/gradle-home "$EMU_IMAGE" bash -c '
  set -e
  for p in platform-tools emulator "${0//\//;}"; do
    [ -f "/opt/android-sdk/${p//;//}/source.properties" ] || sdkmanager --sdk_root=/opt/android-sdk "$p" >/dev/null
    echo "$p $(sed -n "s/^Pkg.Revision=//p" "/opt/android-sdk/${p//;//}/source.properties")"
  done' "$SYSDIR"
docker run -d --name "$name" --device /dev/kvm --group-add "$(stat -c %g /dev/kvm)" --user "$(id -u):$(id -g)" \
  -e HOME=/gradle-home/home -e ANDROID_USER_HOME=/gradle-home/android-user -e ANDROID_AVD_HOME=/avd \
  -e ANDROID_HOME=/opt/android-sdk -e ANDROID_SDK_ROOT=/opt/android-sdk \
  -v k1000-android-sdk:/opt/android-sdk -v k1000-android-gradle:/gradle-home -v k1000-android-avd:/avd \
  -v "$work":/smoke -v "$STATIC":/pwa:ro -v "$here/tools":/tools:ro \
  "$EMU_IMAGE" sleep infinity >/dev/null
WIPE=""
[ "${K1000_SMOKE_WIPE:-0}" = 1 ] && WIPE=-wipe-data
docker exec "$name" bash -c "
  set -e
  if [ ! -f /avd/k1000-$API.avd/config.ini ]; then   # a Pixel-4a-sized AVD, written by hand
    mkdir -p /avd/k1000-$API.avd
    printf 'avd.ini.encoding=UTF-8\npath=/avd/k1000-$API.avd\ntarget=android-$API\n' > /avd/k1000-$API.ini
    printf '%s\n' 'image.sysdir.1=$SYSDIR/' abi.type=x86_64 hw.cpu.arch=x86_64 tag.id=google_apis \
      hw.lcd.width=1080 hw.lcd.height=2340 hw.lcd.density=440 hw.ramSize=3072 disk.dataPartition.size=6G \
      hw.keyboard=yes hw.audioInput=yes hw.camera.back=emulated hw.gpu.enabled=yes hw.gpu.mode=swiftshader_indirect \
      PlayStore.enabled=false hw.mainKeys=no > /avd/k1000-$API.avd/config.ini
  fi
  # A navigation bar (gesture navigation for the edge-swipe check), also on an older AVD.
  grep -q '^hw.mainKeys=' /avd/k1000-$API.avd/config.ini || echo hw.mainKeys=no >> /avd/k1000-$API.avd/config.ini
  rm -f /avd/k1000-$API.avd/*.lock   # left by an emulator that was not shut down
  nohup /opt/android-sdk/emulator/emulator -avd k1000-$API -no-window -no-audio -no-boot-anim -no-snapshot $WIPE \
    -gpu swiftshader_indirect -memory 3072 -cores 4 \
    -netsim-args '--wifi-ssid K1000 --wifi-password abcd-efgh-jkmn' > /smoke/emulator.log 2>&1 &
  timeout 300 adb wait-for-device || { tail -30 /smoke/emulator.log; exit 1; }
  for i in \$(seq 1 180); do [ \"\$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')\" = 1 ] && break; sleep 2; done
  # A cold-booted system can stay busy a while: wait until the Wi-Fi service answers (or give up:
  # an emulator whose system server hangs is the emulator's fault, not the app's).
  for i in \$(seq 1 30); do timeout 15 adb shell cmd wifi status 2>/dev/null | grep -qi wifi && break; sleep 10; done
  timeout 15 adb shell cmd wifi status 2>/dev/null | grep -qi wifi || { echo 'emulator system server not answering: giving up' >&2; exit 1; }
  adb shell getprop ro.build.version.release
  timeout 30 adb shell 'dumpsys package com.google.android.webview | grep -m1 versionName' || true
"
ONLY_ARG=()
[ -n "${K1000_SMOKE_ONLY:-}" ] && ONLY_ARG=(--only "$K1000_SMOKE_ONLY")
docker exec "$name" python3 -I /tools/smoke_driver.py --apk /smoke/app-smoke.apk --work /smoke --static /pwa --api "$API" "${REL_ARG[@]}" "${ONLY_ARG[@]}"
