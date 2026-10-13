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
- **Superseded in part by D-013** (2026-10-13): the button assignments (B1 PAGE · B2 BAND/UNLOCK ·
  B3 NOTCH · B4 MUTE now) and the SETUP list (grouped by speaker floor, choose-then-set).

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

## D-011 — Lo-Cut in 2 Hz steps, one signed sub/top delay, quieter knob tags · decided 2026-10-13

- **Ask** (Thomas at the bench, 2026-10-13): "LO-CUT is moving 6 Hz per click, I'd like 2 Hz
  instead"; "SUB DELAY is only positive value: I can only compensate for front subs, but I'd like to
  be able to go 'negative' which would do tops delay… can you think of something clever to use the
  same setting for either sub or top delay?"; "E1/E2/E3/E4 could be a bit more discreet or maybe
  removed (I could add a sticker on the box)".
- **Proposed by Claude from those words; decided** (Thomas asked for each change). It changes
  D-009's ALIGN E3 and SETUP list; D-010's Lo-Cut default (100 Hz) stays.
  - **Lo-Cut: 2 Hz per click**, 1 Hz with the push held, 20–300 Hz. It was 1/12 octave (6.6 Hz at
    112 Hz). The values live on that grid: a value saved off it (the box holds 112.25 Hz) is not
    rewritten at boot (no engine change), and its first click goes to the next grid line in the
    turn's direction (112.25 → 114 up, → 112 down). Whole Hz on screen; odd values only with the
    push. Fast spins still accelerate, up to ×4 = 8 Hz a click, and only above 20 clicks/s. The
    phone's Lo-Cut buttons become −10 / −2 / +2 / +10 Hz. Every other frequency (live EQ, sub
    low-pass) stays logarithmic.
  - **ALIGN E3 = DELAY: one signed setting for either side**, −50 … +50 ms. **+ = the subs are
    delayed** (subs in front of the tops), **− = the tops are delayed** (tops in front). The screen
    never shows a minus sign: the size big (`1.25 ms`), the side in words under it (`SUBS LATER ·
    0.43 m`, `TOPS LATER · 0.43 m`, `ALIGNED` at 0). Turning through zero stops one click on
    `ALIGNED` before the other side, so the crossing is felt. 0.1 ms a click, one sample with the
    push, no acceleration, the B3-hold lock and the wake-up click as before. Under the hood the two
    stored delays stay (`sub_delay_ms`, `top_delay_ms`: state.json, the engine config, CamillaGUI
    and saved states are unchanged); a move writes the late side and zeroes the other. A state
    with both set (an old SETUP › Top delay, a CamillaGUI edit) shows the net value and is
    normalised by its first move, never at load. Each change dips the group whose delay changes
    (the subs, the tops, or both when one engine write changes both), as the sub delay did.
  - **SETUP › Top delay removed**: one place to set the alignment, one fewer way to get it wrong.
    SETUP has 19 items.
  - **Knob tags dim by default**: k1000.yaml `ui.knob_tags` = `dim` (small, dark grey,
    secondary to the label; the default), `off` (no tag, the label takes the corner: for a sticker
    on the shell) or `bright` (v1's cyan). The knobs stay E1 top-left, E2 bottom-left, E3 top-right, E4
    bottom-right, and the docs keep their names.
  - **Thomas's box: `off`** (Thomas, 13:39 the same day): a sticker on the shell names the knobs.
    Set in the box's `/etc/k1000/k1000.yaml` and in the repo template `box/etc/k1000.yaml`; the
    built-in default (no `ui:` section) stays `dim`, and the manual's pictures show `dim`.
- **Why this delay design**: one knob already guarded (lock, wake-up click, dip) covers both
  layouts, and "which side is late" is the question the operator asks, so the screen answers it in
  words. Two independent delays could both be non-zero, which only adds latency to the whole rig.
- **Pictures**: `docs/ui/align_unlocked.png`, `align_tops_later.png`, `align_aligned.png`;
  `show.png` (dim tags), `show_tags_off.png`, `show_tags_bright.png`, `align_tags_off.png`.
- **Gate**: the Wednesday 14 freeze, like D-009: on the box after the next deploy.
- **Merge note** (`night/snapshots`, venue snapshots, after the gig): it draws its own cyan
  `E1`/`E2` tags (its `_corner` helper and SETUP header), which merge without a conflict and would
  ignore `ui.knob_tags`: route both through `pages._knob_label`. Keep D-011's SETUP list (no Top
  delay). Its venue change lines can word the delay with `fmt_align_delay`.
- **Superseded in part by D-013** (2026-10-13): the SETUP list wording (19 items, now grouped by
  speaker floor: TOPS, SUBS, INPUT, LIVE EQ, PHONE, BOX).

## D-012 — Output patch: out3 feeds both subs, out4 spare · decided 2026-10-13

- **Decision** (Thomas, 2026-10-13): stay on the M4. Outs 1-2 = tops, **out3 = both subs** (they
  are summed anyway), **out4 reserved** for a later, different summed bass voice (to define after
  the gig). Supersedes D-010's output-mapping line.
- **How**: out3 → sub 1's input, then sub 1's Loop output (XLR, in parallel with its input) → sub 2;
  or a plain pin-to-pin XLR Y. Two Meyer inputs in parallel (about 10 kΩ each) are an easy load
  for the M4's line output, and each sub gets the level it had on its own output.
- **Condition**: **Mono subs stays ON.** Out3 carries the L+R sum only then; OFF would put the left
  channel alone on both subs.
- **No processing change**: `output_map` stays `[TopL, TopR, SubL, SubR]`. With Mono subs ON, out4
  carries the same sub sum as out3: a spare sub feed until its own voice exists. The per-sub trims
  and the meter's two sub bars still show, but only out3's matter.
- **Lost**: a separate level, delay or polarity per sub. Fine while they stand together.

## D-013 — SETUP by speaker floor, choose-then-set, buttons B1 PAGE · B2 BAND/UNLOCK · B3 NOTCH · B4 MUTE · decided 2026-10-13

- **Ask** (Thomas at the bench, 2026-10-13): "can you make a pass on the SETUP page: - group things
  per speaker floor instead of function: all things tops, all things subs, ... - Things like
  Audition or Profile, etc: the value changing system is off: E2 says 'Push to change', once
  pushing it shows PUSH AGAIN but the value to select is not visible.. - overhaul that SETUP
  section needs a better organisation". Then: "also i would say: B1 switch between SHOW and ALIGN
  only and B1-hold to get to SETUP page.. what do you think ? Also Move EQ select (B3) into B2,
  move peak detect (B4) into B3, so we can put mute/unmute (B2) into B4".
- **Counsel given** (Claude, to "what do you think?"):
  - **SETUP out of the tap cycle is right.** It is rarely used and holds the risky rows (pipeline
    rebuilds, polarity, the live-EQ reset), so a hold is the right gesture to reach it. One B1 tap
    from ALIGN or SETUP returns to SHOW.
  - **The ALIGN unlock moves to B2 with band select** (Claude's call; the ask did not name it):
    "hold B2 to edit". B2 then always means "what E2–E4 act on", and B3 is the notch alone.
  - **The cost**: relearning all four buttons five days before the gig (the emergency mute moves
    from B2 to B4), relabelling the shell and reprinting the operator card and the quick start.
  - **The Knob test must confirm B1..B4's physical positions** before the shell is labelled, so
    the labels match what the code calls B1..B4.
- **Decided** (Thomas asked for each change; the unlock on B2 is Claude's call): built as below;
  on the box since 14:31 (`dd740ce`, after Thomas's "push the timing … integrate the maximum").
  - **Buttons: B1 PAGE · B2 BAND/UNLOCK · B3 NOTCH · B4 MUTE.**
    - **B1** tap (on release) = SHOW ↔ ALIGN; from SETUP a tap goes to SHOW, from PHONE back to
      SETUP. **B1 hold** (0.8 s) = SETUP from SHOW or ALIGN; from SETUP or PHONE it goes to SHOW.
      The press itself no longer moves a page.
    - **B2** (was B3): SHOW tap = next band, LS 1 2 3 4 HS (wraps). ALIGN: hold = unlock DELAY and
      LO-CUT for 30 s of idle, tap = relock (`LOCKED · HOLD B2 TO EDIT`). Elsewhere a tap says
      `B2: NEXT BAND ON SHOW`.
    - **B3** (was B4): SHOW tap = the notch helper; SHOW hold = `RESET LIVE EQ: IN SETUP`.
      Elsewhere a tap says `B3: NOTCH HELPER ON SHOW`.
    - **B4** (was B2): mute / unmute on a **long press only**, on every page (the Knob test and
      PHONE included), acting on what the screen showed at the press; a tap says
      `HOLD B4 TO MUTE / UNMUTE`. The MUTED band, the PHONE page and the phone's toasts say B4.
    - **The physical map is unchanged**: `controls: buttons: [4, 5, 6, 7]` still maps B1..B4 to
      sw4..sw7, and the Knob test still names sw5 "B2". Only what each logical button does moved.
  - **SETUP grouped by speaker floor**, 19 items as before:
    - **TOPS**: Trim L · Trim R · Polarity · Lo-Cut (on/off; its frequency stays ALIGN E4).
    - **SUBS**: Trim L · Trim R · Polarity · Mono L+R · Low-pass · Low-pass freq.
    - **INPUT**: Trim. **LIVE EQ**: Reset live EQ · Undo reset · Spectrum.
    - **PHONE**: Phone · Laptop access. **BOX**: Profile · Audition (dev) · Knob test.
    - Old labels → new: Input trim → INPUT › Trim, Sub/Top polarity → SUBS/TOPS › Polarity, Sub
      low-pass → SUBS › Low-pass, Sub LP freq → SUBS › Low-pass freq, Trim Sub/Top L/R →
      SUBS/TOPS › Trim L/R, Mono subs → SUBS › Mono L+R, Lo-Cut → TOPS › Lo-Cut.
    - **Knobs**: E1 = item (header rows skipped, clamps), E2 = value, **E3 = group** (forward: the
      next group's first item; back from inside a group: its own first item first), E4 unused.
    - **Screen**: a header row per group, `<GROUP> <pos>/<size>` on the top line, a scroll bar at
      the right edge (no more `▲▼` and `10/19`), and a bottom line saying what the knobs do on the
      selected row (`turn = change`, `turn = choose · push = set`, `push = open`, `push ×2 = run`,
      `fixed here`…).
  - **Choose, then set**, for every ON/off and choice row (both polarities, Lo-Cut, Mono L+R,
    Low-pass, Spectrum, Profile, Audition):
    - **Turn E2 to choose**: one choice per turn, wrapping; back on the current value = nothing
      chosen. The row turns red and shows **now → chosen** (`off → ON`, `SUM → TOPS`); the footer
      says `PUSH TO SET ON`, plus `· MAY CLICK` on a pipeline rebuild (Mono L+R, Profile,
      Audition).
    - **Push E1 or E2 to set it.** With nothing chosen, a push proposes the next value (the row
      shows it) and the next push sets it: push, push still works, with the target in sight.
    - The choice waits 8 s (each turn renews it); E1, E3 or a page change drops it; auto-home
      never fires while a choice or a confirm is pending. A value a phone set meanwhile changes
      nothing: `NO CHANGE: ALREADY SET`.
    - No setting needs the blind double push any more: Lo-Cut loses its confirm (choose + push are
      the two gestures). Reset live EQ and opening Laptop access keep their push twice within 5 s.
  - **Read-only rows**, dimmed, where a turn or push only says why: Profile while k1000.yaml pins
    it (`DEV · PINNED`, `PROFILE PINNED IN k1000.yaml`), Audition off the dev profile (`—`,
    `AUDITION: DEV PROFILE ONLY`), Spectrum while k1000.yaml disables it (`DISABLED`,
    `SPECTRUM OFF IN k1000.yaml`).
- **Why**: one floor's settings sat apart (the tops' Lo-Cut twelve rows below Top polarity), while a
  rig is set floor by floor. The old double push showed `PUSH AGAIN` but not the value it would
  set: on a rebuild, a possible click on a guess. Choose-then-set shows the target before it is set
  and keeps two gestures for everything that rebuilds. The buttons: Thomas's layout, with the
  counsel above.
- **What it supersedes**:
  - **D-009, button assignments**: "band select … on a B3 tap", "B4 tap = notch helper",
    "`LOCKED · HOLD B3 TO EDIT`", "a B3 hold unlocks them", "B1 tap = next page, B1 hold = SHOW".
    Mute stays a long press only, now on B4.
  - **D-009, SETUP list**: "Lo-Cut on/off moves to SETUP with a confirm" → TOPS › Lo-Cut, chosen
    then set. Reset live EQ keeps its confirm and undo.
  - **D-011, SETUP list wording**: "SETUP has 19 items" → still 19, now in six groups; Top delay
    stays gone.
  - Everything else in D-009 (SHOW, the ALIGN lock, auto-home after 60 s, the master-up guard),
    D-011 and D-012 stands. D-012's "Mono subs stays ON" is SETUP › SUBS › Mono L+R ON.
- **Pictures**: `docs/ui/setup.png` (TOPS › Polarity), `setup_confirm.png` (SUBS › Mono L+R,
  `off → ON` chosen), `setup_pick_audition.png`, `setup_profile_pinned.png`, `setup_muted.png`,
  `hwtest_muted.png` (sw7 = B4 held long).
- **Gate**: on the box before the Wednesday 14 freeze, like D-009 and D-011, after the Knob test
  confirms B1..B4. Then the shell labels `B1 PAGE · B2 BAND/UNLOCK · B3 NOTCH · B4 MUTE` and the
  printed cards.
- **Merge note** (`night/snapshots`, venue snapshots, after the gig): it conflicts here. Its SETUP
  items (Venues, and its old flat list) must join a group; its `rebuild` kind becomes `bool` /
  `choice` (`rebuild` is now a property); its VENUES page needs a B1 tap back to SETUP, like PHONE
  (`input_map.B1_TAP`). Its E1/E2 tags go through `pages._knob_label` (D-011). Its hints and docs
  name the D-013 buttons: mute B4, band B2, notch B3.

## D-014 — Each live band IN/OUT: a chip hold on the phone, a B3 hold on SHOW · decided 2026-10-13

- **Ask** (Thomas at the bench, 2026-10-13): "i'd like a way to enable/disable/hold-to-reset on
  each band, maybe using the band selector directly ? or do you have a better UX in mind ?"
- **Answer** (Claude, to "a better UX?"): the band selector itself, as he suggested, with its tap
  kept for selecting; the per-band reset stays FLAT, and the whole reset stays where it is.
- **Decided**: Thomas went with it (he held a chip at the bench before it was built: "Hold a band
  chip: does not seems to work..."). Built as below.
- **The gestures**:
  - **Phone (v1, `/`)**: a band chip **tap = select** (unchanged); a chip **hold 0.6 s = that band
    IN/OUT** (the hold fill, the intent fixed at touch-down); **FLAT** (hold) = the selected band to
    0 dB **and IN**; **ALL FLAT…** = reset all, with its confirm (unchanged). A finger that
    **slides** more than 12 px or off the chip is neither a hold nor a tap (no IN/OUT, no select),
    and a vertical slide still scrolls the page: the chip row is full width, under the graph, where
    a thumb crosses it or reaches up for a dot.
  - **Box**: **B3 hold on SHOW = the selected band IN/OUT** (`P2 OUT` / `P2 IN`), **once the button
    map is confirmed** (below); it only said `RESET LIVE EQ: IN SETUP`. The B3 tap stays the notch
    helper; off SHOW nothing changes.
- **What OUT is**:
  - The band keeps its frequency, gain and Q; the engine plays it at **0 dB** through the same
    smoothing as a gain edit (0.5 dB steps, no rebuild, no click). Back IN brings exactly what was
    there. Saved in state.json (`on`; a file without it loads with every band IN).
  - **Every "audible / in use" question reads the effective gain** (0 dB while OUT): the curve (box
    and phone), the chips' cut/boost colours, the notch helper's "in use" reselect, the reset undo
    (switching a band with a stored cut is an audible edit; at 0 dB it is not).
  - **Free for B3** (and the phone's notch) is the **stored** gain: an OUT bell with a cut keeps its
    settings; an OUT bell at 0 dB is free and comes back IN (silently) when B3 takes it.
  - SETUP › Reset live EQ puts every band IN; its undo puts back what was OUT. A gain set on an OUT
    band from CamillaGUI is what plays, so it is adopted as the band's gain, and the band IN.
- **Screen**: an OUT chip dark with a dashed outline and a small `OUT`, its marker hollow and dashed
  at its stored gain, the curve without it, its values grey; an orange pill `OUT P2` (several:
  `OUT P2 LS`); a knob on an OUT band says `P2 OUT · HOLD B3 = IN` (`P2 OUT · IN FROM THE PHONE`
  while the button map is unconfirmed). The phone: the same chip and dot, a toast `P2 OUT` once
  the box acks.
- **Protocol** (`/ws` stays v1, additive): each `state.bands[i]` carries `on`; `band {s, on}`
  (alone, or with `g` only) goes through `Controller.set_band_on`, logged with its source and said
  on the box screen, validated and rate-limited like the other messages. `on` with `f` or `q` is
  refused, nothing set: the box writes a whole message to the state before the engine worker runs,
  so the engine gets ONE smoothed move whatever the order, and a new frequency would sweep while
  the band fades in or out. With `g` it is a gain move (FLAT = `{g: 0, on: true}` plays 0 dB all
  along). An older page ignores `on`. The v2 protocol (docs/14) adds band on/off later.
- **Why**: IN/OUT is the console's per-band bypass: hear a cut with and without, drop one that went
  wrong without losing it. The chip is where the eye already is; a hold keeps a tap harmless (no
  single tap changes the sound on LIVE) and makes the change deliberate. B3's hold on SHOW was free.
  No separate IN/OUT button: the compacted LIVE EQ page has no room, the box no spare button.
- **Cost, and its gate in code**: a B3 hold now changes the sound. On a swapped button map an
  emergency mute held on "B4" could be a B3 hold, and take the selected band (often the notch
  fighting the ring) OUT instead of muting. So **the B3 hold switches a band only once the map is
  confirmed** (`ControlMap.buttons_confirmed`): `controls: buttons` in k1000.yaml differs from the
  default `[4, 5, 6, 7]` (written by hand after SETUP › Knob test, the rule `k1000-m4-bringup.sh`
  warns on), or the default is kept with `buttons_confirmed: true` beside it (a real YAML bool).
  Until then a B3 hold on SHOW changes nothing and says `B3 HOLD: IN/OUT AFTER KNOB TEST` (it names
  the button the operator actually held), and k1000 logs why at start. The phone's chip hold is not
  gated (no button map there). Lines elsewhere that still say "a B3 hold only shows a hint"
  (`box/README.md`, manual chapters 3 and 6, `hw/mcp23017.py`'s config-guard note) hold only until
  the map is confirmed; `etc/k1000.yaml`'s comment does not name `buttons_confirmed` yet.
- **Pictures**: `docs/ui/show_band_out.png` (P2 OUT and selected), `show_band_out_hint.png`.
- **Supersedes** D-013's "B3 … SHOW hold = `RESET LIVE EQ: IN SETUP`". Everything else in D-013
  stands.
- **Gate**: the Sunday field test, beside the knobs and the bypass.
