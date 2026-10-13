# syntax=docker/dockerfile:1.7
# SPDX-License-Identifier: GPL-3.0-or-later
#
# K1000 Android build image: JDK 17 + the Android SDK command-line tools, pinned.
# The SDK packages themselves (platform, build-tools, platform-tools) and the Gradle caches live in
# Docker named volumes, not in the image (android/build.sh mounts them).

# Temurin 17.0.20+8 on Ubuntu 24.04, pinned by digest (tag published 2026-09-18).
FROM eclipse-temurin:17.0.20_8-jdk-noble@sha256:c2c0deda43a14dea9d5c6c5349d22538c385c7d678d03dc6a133fe0656ed0d64 AS build

# Ubuntu packages pinned too: apt reads the archive as it was at this instant
# (snapshot.ubuntu.com, apt >= 2.7), so a rebuild installs the same versions.
ARG UBUNTU_SNAPSHOT=20260920T000000Z
RUN echo "APT::Snapshot \"$UBUNTU_SNAPSHOT\";" > /etc/apt/apt.conf.d/50k1000-snapshot \
 && apt-get update \
 && apt-get install -y --no-install-recommends unzip python3 \
 && rm -rf /var/lib/apt/lists/*

# Android SDK command-line tools 22.0 (build 15859902, 2026-07-15). The sha256 is the one published on
# developer.android.com/studio; the sha1 in Google's repository2-3.xml is 040d3996a65543d22ec4bf73e4c37aa37a8d4af4.
ADD --checksum=sha256:4e4c464f145a7512b57d088ac6c278c03c9eea610886b35a5e0804e74eedf583 \
    https://dl.google.com/android/repository/commandlinetools-linux-15859902_latest.zip /tmp/cmdline-tools.zip
RUN mkdir -p /opt/android-cmdline-tools \
 && unzip -q /tmp/cmdline-tools.zip -d /opt/android-cmdline-tools \
 && mv /opt/android-cmdline-tools/cmdline-tools /opt/android-cmdline-tools/latest \
 && rm /tmp/cmdline-tools.zip

ENV ANDROID_HOME=/opt/android-sdk \
    ANDROID_SDK_ROOT=/opt/android-sdk \
    GRADLE_USER_HOME=/gradle-home \
    PATH=/opt/android-cmdline-tools/latest/bin:/opt/android-sdk/platform-tools:$PATH

# The emulator smoke test (android/tools/smoke.sh) adds the emulator's runtime libraries on top.
FROM build AS emu
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libpulse0 libnss3 libxcomposite1 libxcursor1 libxi6 libxtst6 libasound2t64 libgl1 libxdamage1 \
      libxkbfile1 libxrandr2 libxkbcommon0 libdbus-1-3 libbsd0 libdrm2 libgbm1 openssl iproute2 \
      python3-websockets \
 && rm -rf /var/lib/apt/lists/*
