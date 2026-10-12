# K1000 — decision log

One entry per decision. States: `proposed` (Claude or anyone suggests) → `decided` (Thomas said so)
→ `superseded by D-NNN`. Newest at the bottom; never rewrite a decided entry, supersede it.

---

## D-001 — The repo is the project workspace · decided 2026-10-12

- **Context**: new project, no code yet; reviews, decisions and progress need a home.
- **Choice**: `Maigre/K1000` (public) holds requirements, reviews (`docs/NN-*.md`), this log and the
  roadmap, then the code. The 37Projects hub keeps only the project card, tasks and log.
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

## D-003 — Audio interface: MOTU M4 · proposed 2026-10-12

- **Why**: bus-powered metal box, ESS DAC, LCD meters on all I/O, Linux-proven, 2 line in + mic with
  phantom at once, €289. Alternative: Focusrite Scarlett 4i4 Gen 4.
- **Caveats**: monitor knob scales outs 1-2 (set full and cap); monitor-mix / input monitoring off.
- **Update 2026-10-12**: Thomas owns no 4-out interface; a **MOTU M4 at ~€150** may be available to
  him. Compared with the Scarlett 4i4 Gen 4 (€285 new): same class of converters for this use; the M4
  wins on bus power (no 2nd brick on a Pi 4), LCD meters on all I/O, and price; the Scarlett wins on
  input headroom (+22 dBu) and full routing/mixer control from Linux (`scarlett2`). Recommendation
  holds: **M4**, checked before buying (all 4 outs, scratch-free pots, solid USB-C, firmware version).
- **Decision**: *pending Thomas.*

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
  - Its private key stays off the box, in the hub's gitignored `secrets/` on mgr-ran.
  - Thomas installs the CA certificate **once** on his phone (Settings → Security → Encryption &
    credentials → Install a certificate → CA certificate). The portal serves it at
    `http://10.42.0.1/k1000-ca.crt`.
  - The leaf is valid until 2029-01-14.

