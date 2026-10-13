#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Builds the K1000 Android app in Docker (JDK 17 + Android SDK, pinned: android/docker/Dockerfile).
# Big caches stay in Docker named volumes: k1000-android-sdk (SDK packages) and
# k1000-android-gradle (Gradle distribution + dependency cache).
#
#   ./build.sh                 lint + unit tests + signed release APK (needs the keystore, below)
#   ./build.sh check           lint + unit tests only
#   ./build.sh gradle <args>   any Gradle command, e.g. ./build.sh gradle assembleDebug
#   ./build.sh keystore        create the release keystore ONCE (refuses to overwrite)
#
# Signing: K1000_SIGNING_DIR (default: the 37Projects hub's secrets/k1000-android) holds
# k1000-release.jks and keystore.properties (mode 600, never in this repo). The release APK goes
# to $K1000_SIGNING_DIR/out/K1000-<versionName>.apk with its .sha256 and apksigner's verdict.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
IMAGE=k1000-android-build:2
SIGNING_DIR=${K1000_SIGNING_DIR:-$HOME/Bakery/37Projects/secrets/k1000-android}
BUILD_TOOLS=36.0.0
# Versioned packages only (sdkmanager serves platform-tools and the emulator at their latest
# revision only: tools/smoke.sh installs those, for the emulator test, never for the build).
SDK_PACKAGES=("platforms;android-37.0" "build-tools;$BUILD_TOOLS")

image() {
  docker image inspect "$IMAGE" >/dev/null 2>&1 || docker build --target build -t "$IMAGE" "$here/docker"
}

run() {  # run <extra docker args...> -- <command...>
  local extra=()
  while [ $# -gt 0 ] && [ "$1" != "--" ]; do extra+=("$1"); shift; done
  shift
  docker run --rm --user "$(id -u):$(id -g)" \
    -e HOME=/gradle-home/home -e ANDROID_USER_HOME=/gradle-home/android-user \
    -v k1000-android-sdk:/opt/android-sdk -v k1000-android-gradle:/gradle-home \
    -v "$here":/src -w /src "${extra[@]}" "$IMAGE" "$@"
}

sdk() {
  docker volume inspect k1000-android-sdk >/dev/null 2>&1 || docker volume create k1000-android-sdk >/dev/null
  docker volume inspect k1000-android-gradle >/dev/null 2>&1 || docker volume create k1000-android-gradle >/dev/null
  docker run --rm -v k1000-android-sdk:/opt/android-sdk -v k1000-android-gradle:/gradle-home "$IMAGE" \
    chown "$(id -u):$(id -g)" /opt/android-sdk /gradle-home
  run -- bash -c 'mkdir -p "$HOME" && (yes | sdkmanager --sdk_root=/opt/android-sdk --licenses >/dev/null) && sdkmanager --sdk_root=/opt/android-sdk "$@" >/dev/null' _ "${SDK_PACKAGES[@]}"
}

gradle() {
  local extra=()
  if [ -f "$SIGNING_DIR/keystore.properties" ]; then
    extra=(-v "$SIGNING_DIR":/signing:ro -e K1000_KEYSTORE_PROPERTIES=/signing/keystore.properties)
  fi
  run "${extra[@]}" -- ./gradlew --no-daemon --console=plain "$@"
}

keystore() {
  if [ -e "$SIGNING_DIR/k1000-release.jks" ] || [ -e "$SIGNING_DIR/keystore.properties" ]; then
    echo "keystore already exists in $SIGNING_DIR: refusing to overwrite it" >&2
    exit 1
  fi
  install -d -m 700 "$SIGNING_DIR"
  # One random password (a PKCS12 keystore uses the store password for the key too). It goes
  # straight into keystore.properties (mode 600) and is never printed.
  run -v "$SIGNING_DIR":/signing -- bash -c '
    set -euo pipefail
    umask 077
    PW=$(head -c 48 /dev/urandom | base64 | tr -d "/+=\n" | head -c 40)
    export PW
    keytool -genkeypair -keystore /signing/k1000-release.jks -storetype PKCS12 \
      -alias k1000 -keyalg RSA -keysize 4096 -sigalg SHA256withRSA -validity 18250 \
      -dname "CN=K1000 Android release, O=Trente-Sept K1000" \
      -storepass:env PW -keypass:env PW >/dev/null 2>&1
    printf "storeFile=k1000-release.jks\nstorePassword=%s\nkeyAlias=k1000\nkeyPassword=%s\n" "$PW" "$PW" \
      > /signing/keystore.properties
  '
  chmod 600 "$SIGNING_DIR/k1000-release.jks" "$SIGNING_DIR/keystore.properties"
  echo "created $SIGNING_DIR/k1000-release.jks and keystore.properties (mode 600)"
}

release() {
  [ -f "$SIGNING_DIR/keystore.properties" ] || { echo "no keystore: ./build.sh keystore first" >&2; exit 1; }
  gradle lint test assembleRelease
  local name apk out
  name=$(sed -n 's/.*versionName = "\(.*\)".*/\1/p' "$here/app/build.gradle.kts" | head -n 1)
  apk="$here/app/build/outputs/apk/release/app-release.apk"
  out="$SIGNING_DIR/out"
  install -d -m 700 "$out"
  cp "$apk" "$out/K1000-$name.apk"
  (cd "$out" && sha256sum "K1000-$name.apk" > "K1000-$name.apk.sha256")
  # The notices and licences travel with the APK (also inside it: assets/licenses, About).
  cat "$here"/app/src/main/assets/licenses/{NOTICE,GPL-3.0,Apache-2.0}.txt > "$out/K1000-$name.LICENSES.txt"
  run -v "$out":/out -- /opt/android-sdk/build-tools/$BUILD_TOOLS/apksigner verify --verbose --print-certs \
    "/out/K1000-$name.apk" | tee "$out/K1000-$name.apk.verify.txt"
  cat "$out/K1000-$name.apk.sha256"
}

image
case "${1:-release}" in
  sdk) sdk ;;
  check) sdk; gradle lint test ;;
  gradle) shift; gradle "$@" ;;
  keystore) image; keystore ;;
  release) sdk; release ;;
  *) echo "usage: $0 [release|check|gradle <args>|keystore|sdk]" >&2; exit 2 ;;
esac
