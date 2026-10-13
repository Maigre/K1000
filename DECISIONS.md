# K1000 — decision log

One entry per decision. States: `proposed` (Claude or anyone suggests) → `decided` (Thomas said so)
→ `superseded by D-NNN`. Newest at the bottom; never rewrite a decided entry, supersede it.

---

## D-001 — The repo is the project workspace · decided 2026-10-12

- **Context**: new project, no code yet; reviews, decisions and progress need a home.
- **Choice**: `Maigre/K1000` (public) holds requirements, reviews (`docs/NN-*.md`), this log and the
  roadmap, then the code. The author's private notes keep only the project card, tasks and log.
- **By**: Thomas, at the project's creation.

## D-002 — Architecture: D1, CamillaDSP on the Zynthian V4 · decided 2026-10-12

- **Context**: `docs/01-approaches-review.md` compared eight approaches; Thomas's answers
  (`docs/requirements.md` §5: USW-1P, 4 outs, no rack/compact, knobs first, Android, DIY welcome,
  gig 2026-10-18) narrow them (`docs/02-plan-to-sunday.md` §1).
- **Proposal**: D1 — the owned Zynthian V4 (Pi 4, 3.5″ screen, 4 encoders + 4 buttons) + a 4-out
  class-compliant USB interface + CamillaDSP; a small control service maps the knobs and serves the
  phone. Final hardware shape revisited after the first outing.
- **Rejected for now**: 19″ processors (no rack); compact DSP boxes (no live-EQ controls on the box);
  mixers (bulk) — the A&H CQ-18T stays the ready-made fallback.
- **Decision**: **DIY v0** for the 2026-10-18 gig — Thomas, 2026-10-12.

## D-003 — Audio interface: MOTU M4 · decided 2026-10-12

- **Why**: bus-powered metal box, ESS DAC, LCD meters on all I/O, Linux-proven, 2 line in + mic with
  phantom at once, €289. Alternative: Focusrite Scarlett 4i4 Gen 4.
- **Caveats**: MONITOR knob scales outs 1-2 (fully clockwise, taped); INPUT MONITOR MIX is a balance
  knob: fully clockwise = playback only (taped), MON buttons unlit, or raw input leaks into outs 1-2.
- **Update 2026-10-12**: Thomas owns no 4-out interface; a **MOTU M4 at ~€150** may be available to
  him. Compared with the Scarlett 4i4 Gen 4 (€285 new): same class of converters for this use; the M4
  wins on bus power (no 2nd brick on a Pi 4), LCD meters on all I/O, and price; the Scarlett wins on
  input headroom (+22 dBu) and full routing/mixer control from Linux (`scarlett2`). Recommendation
  holds: **M4**, checked before buying (all 4 outs, scratch-free pots, solid USB-C, firmware version).
- **Decision**: Thomas, 2026-10-12: the ~€150 M4, picked up Tue 13 with the checks above. Confirmed
  as the Sunday interface in D-010.

## D-004 — Engine for v0: CamillaDSP v5 beta · decided 2026-10-12

- **Why**: v5.0.0-beta.1 (2026-10-08) brings the spectrum analyzer and lookahead limiter, but a beta
  does not go on a gig on 2026-10-18. Move to v5 after the first outing (configs and websocket client
  change shape).
- **Thomas asked "why not v5?"** (2026-10-12). For v5: fixes RT priority (v4: set it via systemd —
  equivalent), lookahead limiter (not needed Sunday: the Meyers limit themselves), spectrum (not in
  v0 scope), faster biquads (irrelevant at ~2 % CPU), no migration later. Against: 4 days old beta,
  breaking config/websocket changes, no pycamilladsp v5 yet, CamillaGUI v5 backend rewritten in
  Rust 2 days ago — new code on every layer we'd stand on during the gig. The control service is
  written so the v5 move is a small adapter change.
- **Decision**: **CamillaDSP v5 beta** (latest beta at install time, then frozen for the gig) —
  Thomas, 2026-10-12, over the v4.1.3 recommendation. Consequences: config written in the v5 format;
  the control service speaks the v5 websocket directly (no pycamilladsp v5 yet); CamillaGUI v5 beta
  for the phone; the spectrum becomes available early (stretch, not v0 scope); the soak test and the
  bypass kit carry the beta risk.

## D-005 — Base OS: fresh Raspberry Pi OS Lite (64-bit) · decided 2026-10-12

- **Context**: ZynthianOS works out of the box with the V4 screen and encoders but brings JACK and a
  synth stack K1000 doesn't use; a plain Pi OS left one user's V4 screen dark (zynthian forum,
  2025-09) because the display overlay is Zynthian-specific.
- **Choice**: Thomas's call, agreed: a **fresh Raspberry Pi OS Lite 64-bit** on a **new** SD card —
  lean, our stack only, ready for a read-only root later. The Zynthian's own card is kept untouched:
  it is the fallback and the reference for the display/encoder config (`config.txt`, overlays).
- **Hardware bring-up to port** (from `zynthian-sys` V4 envars): display `piscreen2r-notouch,rotate=270`
  (SPI, 480×320), resistive touch ADS7846 on CE1 / pen IRQ GPIO17, backlight GPIO22; 4 encoders +
  4 switches on an MCP23017 at I²C 0x20 with INTA/INTB interrupts.
- **Fallback**: if the screen fights past Tuesday, boot the ZynthianOS card with its services
  disabled.


## D-006 — Reaching Lite: converge the card in place now, fresh-Lite gig card via a bootstrap · decided 2026-10-12

- **Context**: D-005 said Lite, but the card was flashed with the **desktop** image ("Raspberry Pi
  reference 2026-10-06", pi-gen `b2cd98a9`, `stage4`; 1649 packages). Thomas wants a minimal Lite with
  the best performance and boot time. At that commit the desktop is exactly Lite (`stage2`) plus ten
  metapackages: `rpd-preferences rpd-theme rpd-wayland-core rpd-x-core` (stage3) and `rpd-applications
  rpd-developer rpd-graphics rpd-utilities rpd-wayland-extras rpd-x-extras` (stage4).
- **Proposal**:
  1. **Now, remotely** — purge those ten + `autoremove --purge`. This converges to the same build's
     Lite package set. Simulate first, and refuse any plan that touches ssh, NetworkManager or the
     kernel/firmware.
  2. **Then tune the boot beyond Lite**, since performance and boot time come from what *runs*, not
     what is installed. Lite still ships cloud-init (2.6 s on k1000's critical path),
     NetworkManager-wait-online (4.3 s), bluetooth, avahi, rpcbind/nfs, the apt/man-db timers, a
     boot-time EEPROM check and the swap resize.
  3. **Later, with Thomas at the box** — write a fresh Lite on a second card and run
     `box/bootstrap.sh` (fresh Lite + key → complete K1000). That proves the build is reproducible
     and gives a **spare card for the gig case**. Then pick which card is primary.
- **Why not only a fresh flash now**: it needs Thomas's hands on the card and repeats the bring-up,
  and the end state is the same package set.
- **Decision**: as proposed, in that order — Thomas, 2026-10-12 ("i validate everything").

## D-007 — Phone access: the box's own Wi-Fi AP, QR code on screen, captive portal → a K1000 phone PWA · decided 2026-10-12

- **Context**: at gigs there is no house network. Thomas's idea: the Pi runs an access point, the
  screen shows a QR code to join it, and a captive portal opens the interface. The plan had put the
  box's own AP after Sunday (`docs/02-plan-to-sunday.md` §3).
- **Proposal** (Thomas's idea, with refinements):
  - **AP**: NetworkManager on the onboard `wlan0`; `eth0` is untouched. WPA2 with a random password
    that is shown only on the box screen, so physical access is the authorisation. Country FR,
    5 GHz ch 36 by default.
  - **QR**: a Wi-Fi QR (`WIFI:T:WPA;S:…;P:…;;`), which Android 10+ joins in one tap. Once a phone
    holds a DHCP lease, the screen shows a second QR with the URL.
  - **Captive portal**: dnsmasq answers every name with the box, and a small HTTP server on :80 makes
    Android raise "Sign in to network". This is a bonus, not the main path: Android's sign-in window
    is a stripped browser that may close, and a "no internet" network can push traffic to mobile
    data. The robust gig setup is the phone in airplane mode with Wi-Fi on.
  - **The page**: K1000's own, served by the k1000 service (python3-websockets, no new dependency),
    driving the same controller as the knobs (same clamps, same click-safe moves, one state). It is
    not CamillaGUI, which POSTs whole configs and can undo knob work. Scope: MAIN (master, mute,
    balance) and LIVE EQ (notch slots: frequency/gain/Q). B2 on the box stays the master mute, and a
    SETUP toggle switches the phone off.
- **Gate**: built before Friday, alongside the integration. Go/no-go at the 16/10 rig test; if it is
  not proven there, it is off on Sunday and the knobs carry the show.
- **Decision**: as proposed, defaults included (FR, SSID `K1000`, 5 GHz, AP on at boot with an off
  switch in SETUP) — Thomas, 2026-10-12. He added: **make the phone interface a PWA**, for a quick
  way in.
- **PWA ⇒ HTTPS.** Chrome on Android installs a PWA only from a secure origin, and
  `http://10.42.0.1` is not one. So the box serves `https://10.42.0.1/` with a leaf certificate
  signed by a **local CA**:
  - The CA is *name-constrained* (critical): it can vouch only for IP 10.42.0.0/24 and
    `k1000.home.arpa`, so even a leaked key cannot impersonate another site to the phone.
  - Its private key stays off the box, on the laptop that made it, outside any repository.
  - Thomas installs the CA certificate **once** on his phone (Settings → Security → Encryption &
    credentials → Install a certificate → CA certificate). The portal serves it at
    `http://10.42.0.1/k1000-ca.crt`.
  - The leaf is valid until 2029-01-14.
- **Update 2026-10-12 (evening), implementation defaults after the phone review:**
  - **The AP runs on hostapd, PMF unset, not on NetworkManager's AP.** Verified in the author's
    notes (2026-09-03): on the Pi 4's own Broadcom chip, NetworkManager's
    wpa_supplicant AP completes the handshake and then drops Apple and older-Android clients about
    6 s later, before DHCP. NetworkManager leaves wlan0 alone; eth0 is untouched.
  - **Captive DNS off by default.** With it on, Android never offers "stay connected" and pauses
    auto-join after each automatic join, so a phone that drops mid-show may not come back by itself.
    The box screen gets a third QR, "FIRST TIME" (http://10.42.0.1/), for the one-time CA install.
    `setup-ap.sh --portal on` brings the catch-all back.
  - **A network fence**: phones on the AP reach only DHCP, DNS, 80, 443 and 8080 (the page's plain
    fallback), and ping; no CamillaGUI, no SSH (`--ssh on` opens it), no forwarding.
  - **The phone can unmute**, guarded: an unmute that saw an older mute state is refused.
  - **No-go on Friday = the AP fully off**, not only the page: `sudo /opt/k1000/net/setup-ap.sh
    --down` (off the air, across reboots and later `install.sh --ap` runs) **and**
    `phone: enabled: false` in `k1000.yaml` (no phone servers), then restart k1000 between shows.
    `phone.enabled: false` alone leaves the Wi-Fi on the air (`docs/06-phone.md` §9).
  - The phone stays **portrait** for Sunday. An Android shell APK and a landscape EQ layout come
    after the gig (`ROADMAP.md` Phase 2).
  - Thomas's phone is a Pixel 4a. He stands up to ~10 m from the box: 5 GHz ch 36 first, 2.4 GHz
    as the fallback.


## D-008 — Licence: GNU GPLv3 (or later) · decided 2026-10-12

- **Decision**: Thomas, 2026-10-12: "The License is GNU GPLv3 (if compatible with other components
  used here)". Applied as **GPL-3.0-or-later** (the FSF's recommended form), with `LICENSE` holding
  the verbatim text and an SPDX header in each source file.
- **Compatibility check** (licence files on the box, 2026-10-12):
  - **Compatible**: CamillaDSP and CamillaGUI (GPL-3.0); websockets, numpy (BSD-3); PyYAML,
    smbus2 (MIT/Expat); Pillow (HPND); qrcode (BSD); python3-libgpiod (LGPL-2.1+).
  - **Not compatible**: `python3-smbus`. Its `py-smbus/smbusmodule.c` is **GPL-2 only**, so K1000
    moves to **python3-smbus2** (MIT; same `SMBus` API).
  - **Kept under its own licence**: `box/overlays/k1000-zynscreen.dts`, derived from the kernel's
    `piscreen2r` overlay, stays **GPL-2.0**. It is a separate firmware artefact, never linked with
    the Python code.
- Nothing third-party is vendored; fonts (DejaVu) are used from the system.

## D-009 — Live UI v1: one SHOW page with every live EQ control, alignment off the front · decided 2026-10-12

- **Ask** (Thomas, after using the v0 panel): take sub delay and Lo-Cut off the front page ("it
  might be a risky mistake to touch"); put every live control on one page; show the applied EQ over
  a live spectrum like a pro console, with LS · EQ1–4 · HS selection; maybe a GEQ toggle. Also: the
  knobs feel imprecise when turned fast.
- **Design**: `docs/07-live-ui-v1.md` (pro-console research, mockups in `docs/ui/v1/`, engine and
  panel probes), critiqued from a FOH engineer's seat. Knob analysis: `docs/08-knob-precision.md`.
- **Decided** (Thomas, 2026-10-12, from four options each):
  - **SHOW** is the front page: E1 master, E2/E3/E4 = frequency/gain/Q of the selected band, band
    select LS · 1 · 2 · 3 · 4 · HS on a B3 tap, the EQ curve (computed from the engine's own filters)
    over the program spectrum. The encoder push stays "fine".
  - **The sub/top balance lives on ALIGN only**, not on SHOW.
  - **B4 tap = notch helper**: it selects a free band at a notch Q (about 1/6 octave), at the
    spectrum peak if there is one, and never changes gain, so it is inaudible until E3 cuts. **Reset
    all live EQ moves to SETUP**, with a confirm and a one-step undo.
  - **ALIGN** (master, balance, sub delay, Lo-Cut) is **locked**: a turn on the alignment knobs shows
    "LOCKED · HOLD B3 TO EDIT", and a B3 hold unlocks them for 30 s. Master and balance stay live.
    Lo-Cut on/off moves to SETUP with a confirm.
  - **Sunday is DJ/program only**: the spectrum shows the music, not the room. It is built behind a
    SETUP switch and decided at Friday's rig test.
- **Defaults taken with them** (from the research and the critique):
  - No EQ in/out (bypassing would bring rings back) and no GEQ (on four encoders it is slower and
    coarser than parametric bands).
  - B1 tap = next page, B1 hold = SHOW, and ALIGN/SETUP return to SHOW after 60 s idle.
  - Knob decoder fixed (patches A + B), and acceleration only on genuinely fast spins, never on
    master up.
  - A master-up guard against a wrong-knob sweep.
  - Shorter fader ramps so the master answers the hand.
  - System alerts get their own place and colour, apart from hints.
- **Gate**: accepted on the box by the Wednesday 14 evening freeze, otherwise Sunday runs v0. Thursday's
  soak runs v1 with the spectrum on.

## D-010 — Sunday settings, and the bypass as the only fallback · decided 2026-10-13

- **Ask**: the open questions in the morning brief after the night build (Thomas, 2026-10-13).
- **Decided by Thomas**:
  - **Mono subs ON** (L+R sum into both subs). It is a saved setting, already ON on the box; the
    M4-day output test turns it off for its own check and back on.
  - **The bypass is the only fallback.** No software fallback to the v0 UI (`f7aa58b`): if K1000
    misbehaves at the rig test or the gig, the bypass cabling goes in. `/opt/k1000.prev` stays the
    rollback for a bad deploy only.
  - **Spectrum ON** for Sunday (D-009 had left it to Friday's test). Friday checks it, and turns it
    off only if something is actually wrong.
  - **Top Lo-Cut default 100 Hz**, 12 dB/oct (already the code default). Goal: keep the sub-bass the
    UPA-1P cannot use out of it without thinning its low end. 160 Hz (the LD-2 practice) stays a
    Friday option only if the tops limit early. A 12 dB/oct cut at 100 Hz is −3 dB at 100 Hz and
    about −5 dB at 80 Hz, so it does hand the bottom octave to the subs: check the sum there with
    the mic (docs/02 step 4).
  - Output mapping: no preference, so the recommendation stands: out1/2 tops, out3/4 subs.
  - Licence: GPL-3.0-or-later (D-008) confirmed.
  - `CLAUDE.md` stays in the public repo.
  - No mobile data needed on the phone at the gig. Changing the box password can wait (SSH is
    key-only).
- **Decided by Claude** (Thomas: "I don't know"): **M4 `target_level` 512 frames** (~10.7 ms) in
  `profiles: m4:`. Sunday is DJ/program only, so the extra 5 ms of latency costs nothing, and the
  margin against a USB underrun is worth more. It is set in the repo's `box/etc/k1000.yaml` and on the
  box's `/etc/k1000/k1000.yaml`; it takes effect at the first engine start on the M4. Thursday's
  overnight soak runs at 512. Go to 768 if the M4-day soak shows an underrun. Go back to 256 only if
  live mics with stage monitors ever join.
