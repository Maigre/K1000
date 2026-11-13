# SPDX-License-Identifier: GPL-3.0-or-later
# Cross-build of CamillaDSP for the K1000 box (Raspberry Pi 4, Raspberry Pi OS trixie, arm64).
# Debian trixie on purpose: the binary links against the same glibc and libasound as the box.
# Pinned, so that build.sh gives the measured binary (its PIN_SHA256) again later and elsewhere:
# the base image by digest, and the Rust toolchain that made the measured binary
# (`readelf -p .comment out/camilladsp` names it). The apt packages are not pinned; the measured
# build had gcc-aarch64-linux-gnu 4:14.2.0-1, binutils-aarch64-linux-gnu 2.44-3,
# libc6-dev-arm64-cross 2.41-11cross1 and libasound2-dev 1.2.14-1+deb13u1.
FROM debian:trixie-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f
ENV DEBIAN_FRONTEND=noninteractive
RUN dpkg --add-architecture arm64 \
 && apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git pkg-config build-essential \
      gcc-aarch64-linux-gnu libc6-dev-arm64-cross libasound2-dev:arm64 \
 && rm -rf /var/lib/apt/lists/*
ENV RUSTUP_HOME=/opt/rustup CARGO_HOME=/opt/cargo PATH=/opt/cargo/bin:$PATH
RUN curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain 1.99.1 \
      --target aarch64-unknown-linux-gnu \
 && chmod -R a+rwX /opt/rustup /opt/cargo
ENV CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=aarch64-linux-gnu-gcc \
    PKG_CONFIG_ALLOW_CROSS=1 \
    PKG_CONFIG_LIBDIR=/usr/lib/aarch64-linux-gnu/pkgconfig:/usr/share/pkgconfig
WORKDIR /src
