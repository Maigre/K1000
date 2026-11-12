# SPDX-License-Identifier: GPL-3.0-or-later
# Cross-build of CamillaDSP for the K1000 box (Raspberry Pi 4, Raspberry Pi OS trixie, arm64).
# Debian trixie on purpose: the binary links against the same glibc and libasound as the box.
FROM debian:trixie-slim
ENV DEBIAN_FRONTEND=noninteractive
RUN dpkg --add-architecture arm64 \
 && apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git pkg-config build-essential \
      gcc-aarch64-linux-gnu libc6-dev-arm64-cross libasound2-dev:arm64 \
 && rm -rf /var/lib/apt/lists/*
ENV RUSTUP_HOME=/opt/rustup CARGO_HOME=/opt/cargo PATH=/opt/cargo/bin:$PATH
RUN curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain stable \
      --target aarch64-unknown-linux-gnu \
 && chmod -R a+rwX /opt/rustup /opt/cargo
ENV CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=aarch64-linux-gnu-gcc \
    PKG_CONFIG_ALLOW_CROSS=1 \
    PKG_CONFIG_LIBDIR=/usr/lib/aarch64-linux-gnu/pkgconfig:/usr/share/pkgconfig
WORKDIR /src
