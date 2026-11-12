#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Build the K1000 engine: CamillaDSP at a pinned upstream commit, for the box (arm64), on any
# machine with Docker. D-019: upstream's fix for the delay that stays after a dropout is on its
# `next5` branch and in no release yet, so the box runs a build of that branch.
#
#   ./build.sh [COMMIT] [OUT_DIR]      default COMMIT: the pin below; default OUT_DIR: ./out
#
# Result: OUT_DIR/camilladsp (the binary; it reports its commit: `camilladsp --version`) and
# OUT_DIR/Cargo.lock (the dependency versions it was built with: upstream commits no lock file,
# so the lock of the measured build is kept beside this script as Cargo.lock.<commit>).
# Install on the box: copy the binary to /usr/local/bin/camilladsp (mode 755), restart camilladsp.
#
# The pin. Bench of 2026-11-12 on the box (docs/17): 0930f9b reaches 5 to 8 ms in to out where
# 5.0.0-beta.1 never got under 12.5 ms, and the delay comes back after an underrun.
set -eu
PIN=0930f9bbfefa34a7b47c47497575f231dbe22b48
# sha256 of the binary this script gives for the pin (built twice on 2026-11-12, same result);
# it is the binary that was measured on the box.
PIN_SHA256=dd4106c927a5de2b31776cc28d797a3f42fb9b7d02deb928f45a6feee057d43c
commit=${1:-$PIN}
out=${2:-$(pwd)/out}
here=$(cd "$(dirname "$0")" && pwd)
url=https://github.com/HEnquist/camilladsp.git
mkdir -p "$out"
out=$(cd "$out" && pwd)
if [ ! -d "$out/src/.git" ]; then
  git init -q "$out/src"
  git -C "$out/src" remote add origin "$url"
fi
git -C "$out/src" fetch -q --depth 1 origin "$commit"
git -C "$out/src" checkout -q --detach FETCH_HEAD
test "$(git -C "$out/src" rev-parse HEAD)" = "$commit"
# The dependency versions of the build that was measured, when this commit has them here:
# the same lock gives the same engine. Without it, cargo resolves the newest that fit.
short=$(printf %s "$commit" | cut -c1-7)
locked=
if [ -f "$here/Cargo.lock.$short" ]; then
  cp "$here/Cargo.lock.$short" "$out/src/Cargo.lock"
  locked=--locked
fi
docker build -t k1000-cdsp-cross:trixie "$here" 2>&1 | tail -n 3
docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -e LOCKED="$locked" \
  -v "$out/src":/src -v "$out":/out -e CARGO_TARGET_DIR=/out/target \
  k1000-cdsp-cross:trixie \
  sh -c 'rustc --version && cargo build --release $LOCKED --target aarch64-unknown-linux-gnu 2>&1 | tail -n 5'
cp "$out/target/aarch64-unknown-linux-gnu/release/camilladsp" "$out/camilladsp"
cp "$out/src/Cargo.lock" "$out/Cargo.lock"
sum=$(sha256sum "$out/camilladsp" | cut -d" " -f1)
echo "$sum  $out/camilladsp"
if [ "$commit" = "$PIN" ]; then
  if [ "$sum" = "$PIN_SHA256" ]; then echo "this is the binary measured on the box (docs/17)"
  else echo "NOT the measured binary (expected $PIN_SHA256): a different toolchain or lock; measure it before a show" >&2; fi
fi
