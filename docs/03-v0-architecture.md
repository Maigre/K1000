# 03 — v0 architecture (the build contract, 2026-10-12)

The contract every v0 module is built against. Scope and knob plan: `02-plan-to-sunday.md`.
Decisions: D-002 (Zynthian V4 + CamillaDSP), D-004 (CamillaDSP **v5.0.0-beta.1**), D-005/D-006 (Pi OS Lite).

**Corrected to the build, 2026-10-12 evening** (`e3a3deb` + the fix round of the same day). What the
build proved different is fixed in place below and listed in §7. Operator view:
`04-operator-card.md` · M4 bring-up: `05-m4-day.md` · deploy and ops: `box/README.md`.

**UI v1 (D-009), accepted on the box 2026-10-13**: the panel pages, buttons, fader ramp, master-up
guard and the SHOW spectrum changed this contract. They are fixed in place below, and §8 lists them.
**D-011 (same day)**: the Lo-Cut moves 2 Hz a click, ALIGN's DELAY knob (E3 then, E2 since D-015)
is one signed sub/top delay (SETUP has no Top delay), and the E1–E4 tags follow k1000.yaml
`ui.knob_tags`; fixed in place below.
**D-013 (same day) supersedes the buttons and SETUP**: B1 PAGE · B2 BAND/UNLOCK · B3 NOTCH ·
B4 MUTE (a B1 tap flips SHOW ↔ ALIGN, a hold opens SETUP; mute moves to B4, band and unlock to B2,
the notch helper to B3), and SETUP is grouped by speaker floor with choose-then-set. Fixed in place
in §2–§4; §7 keeps the v0 comparison, and §8 says where D-013 changed D-009.
**D-015 (2026-10-14) moves knob roles**: SHOW E3 = Q (a shelf's slope), E4 = GAIN, so the notch
hint says `E4 TO CUT`; ALIGN E2 = DELAY, E3 = BALANCE, and the locked pair (DELAY, Lo-Cut) is
E2/E4. The screen puts each value flush to its corner and adds the IN L/R input meters. Fixed in
place in §4's knob table and buttons; §8 keeps the D-009 history.

## 1. Proven hardware facts (on the box, 2026-10-12)

| Item | Fact |
|---|---|
| Board | Raspberry Pi 4 Model B Rev 1.4, 8 GB; Raspberry Pi OS **trixie** 64-bit, kernel 6.18, Python **3.13** |
| Boot (headless) | `multi-user.target`, PipeWire/WirePlumber masked. Measured after a reboot: systemd userspace 15.0 s; k1000 connected to the engine at 11.7 s (monotonic) and ramped Main −40 → −20 dB over ~3 s; engine load ~1.1 % on the real `dev` pipeline; SoC 49.6 °C, throttled 0x0 |
| Audio (`dev` profile) | HiFiBerry DAC+ADC Pro, auto-loaded from its HAT EEPROM. ALSA card **`sndrpihifiberry`** (`hw:sndrpihifiberry`). Playback 2 ch S16/S24/S32_LE, capture 2 ch S16/S32_LE, 48 kHz OK |
| Audio (`m4` profile) | MOTU M4 (arrives 2026-10-13). Research, **UNVERIFIED on the unit**: card id **`M4`**; 4 in / 4 out (UCM `M4-HiFi.conf`); capture 0/1 = front combo inputs (preamps), 2/3 = rear LINE IN 3/4; playback 0/1 = outs 1-2 (scaled by the front MONITOR knob; phones mirror them), 2/3 = outs 3-4 (fixed level); S32_LE; the first hardware revision (USB 07fd:0008) appears ~4 s after plugging. Channel counts are still **probed**, not assumed |
| Screen | ZynScreen 3.5″ = ILI9486 via `fb_ili9486` → **`/dev/fb0`**, 480×320, **RGB565** (16 bpp, stride 960). Overlay `box/overlays/k1000-zynscreen.dts`. Backlight lit when GPIO22 is HIGH. The Linux console binds to it: `k1000.service`'s root `ExecStartPre` unbinds every vtcon named "frame buffer". fbcon can still win the boot race if fb0 registers after that (open, §7) |
| Touch | ADS7846 on SPI0 CE1 → input device "ADS7846 Touchscreen" (not used in v0) |
| Knobs | MCP23017 at **I²C bus 1, 0x20**. Pull-ups must be enabled (GPPUA/GPPUB = 0xFF). **INTA → BCM GPIO27, INTB → BCM GPIO4**, active-low (proven by forced interrupts) |
| Encoders (A, B pins) | enc0 = GPA2, GPA1 · enc1 = GPA5, GPA4 · enc2 = GPB2, GPB1 · enc3 = GPB5, GPB4 |
| Switches (active-low) | sw0..3 = encoder pushes GPA0, GPA3, GPB0, GPB3 · sw4..7 = buttons GPA6, GPA7, GPB6, GPB7. **Physical positions still unknown** → mapping is config (`controls:`), identified with SETUP › BOX › Knob test |
| Engine | CamillaDSP **5.0.0-beta.1** at `/usr/local/bin/camilladsp`; websocket v5 protocol (`{"command": X, ...}` → `{"reply": X, "result": "Ok", "value": ...}`) |
| Engine facts measured by the build | A negative gain must touch its flag: **`-g-40`** (`-g -40` is an unknown option). A fresh engine reads Main muted at −40 dB and every Aux fader at 0 dB. A Delay with `subsample: false` is a whole number of samples, rounded to nearest (0.0104 ms → 0, 0.0105 ms → 1), and every Delay write rebuilds the delay line (a click). **Any** `devices` change, even `volume_limit` alone via SetConfigValue, restarts capture and playback. A fader change made while the engine is not Running is applied as a step when audio resumes. Started from a file on an absent device, the engine exits 0 within ~20 ms |
| Python deps (apt) | python3-websockets 15, python3-smbus2 (D-008: python3-smbus is GPL-2.0-only), python3-libgpiod (gpiod **2.2** API), python3-yaml, python3-pil, python3-numpy, python3-pytest |

Reference docs (v5, downloaded): CamillaDSP `README.md`, `websocket.md`, `backend_alsa.md`, `CHANGELOG.md`.

## 2. Process model

```
camilladsp.service ── sh: wait for a valid active.yml (camilladsp -c) and for every hw:<card> it names
       │               in /proc/asound (1 s poll, 30 s cap), then
       │               exec camilladsp -m -g-40 -p 1234 -a 127.0.0.1 -o /var/log/k1000/camilladsp.log
       │                    --log_rotate_size 256000 --log_keep_nbr 50 /var/lib/k1000/active.yml
       ▲  websocket 127.0.0.1:1234   (boots MUTED at −40 dB; exits 0 whenever processing stops → restarted in 1 s)
       │
k1000.service ── python3 -m k1000   (the control service: state, knobs, screen, persistence)
       │  writes /var/lib/k1000/state.json + /var/lib/k1000/active.yml (atomic)
       │  UI v1: a SECOND websocket to :1234 for the SHOW spectrum only (rta.py, §8)
       │
camillagui.service ── CamillaGUI v5 beta.2 on :5005 (phone compact view, laptop editor), same websocket
```

- **Audio never depends on the UI.** If `k1000` dies or stops, CamillaDSP keeps playing as it was:
  k1000 never mutes on its way out. If CamillaDSP (re)starts, it comes up **muted** (`-m`) at −40 dB;
  `k1000` detects it, re-applies, and ramps in. No wait mode (`-w`): there a lost device leaves the
  engine alive but Inactive forever; started from the file, it exits and systemd restarts it.
- **A missing device is waited for before the exec** (the same `/proc/asound/<id>` entry k1000 reads
  for NO DEVICE): no restart or log-rotation churn while the M4 is absent, audio within ~1 s of the
  card appearing, and the 30 s cap means a wrong check can never hold audio back longer.

### Boot of a fresh engine (`Controller._sync_fresh` → `_boot_ramp`)

| # | Step |
|---|---|
| 1 | camilladsp starts muted at −40 dB from `active.yml` (the saved state baked in) |
| 2 | k1000 connects and sees a **fresh engine**: no config, or Main muted at −40 dB (±0.05) and no Aux4 marker |
| 3 | Pushes the saved state: a whole upload when the title (`K1000 <profile>`), the devices or the structure differ, else one PatchConfig of the values |
| 4 | Parks Aux1/Aux2 at the balance and Main at the boot gain (or at the master, if lower), still muted; sets Main's marker bit |
| 5 | **Waits until GetState reads Running**, twice, 12 ms apart (> 2 chunks). No timeout. If it has to wait: alert `NO AUDIO FLOWING · HELD MUTED`. The MUTED band says `RAMPS IN BY ITSELF · hold B4 to keep muted` throughout |
| 6 | Unmutes, then steps Main to the saved master over `boot_ramp_s` (3 s): one step per engine fader ramp (100 ms; 300 ms until UI v1), ≤ 6 dB per step, Running re-checked before each. Engine stops mid-ramp → Main muted and re-parked, the ramp restarts once audio flows |
| 7 | Clears Main's marker bit |

- A state saved **muted** parks Main at the master and stays muted (`READY · hold B4 to unmute`).
- The operator wins: B4 during the ramp drops its unmute; a master turned down caps the steps.
- `engine.boot_gain_db` (clamped −80…−20) and `engine.boot_ramp_s` (0.5…30) come from k1000.yaml.

### Recognising the engine on every (re)connect: the Aux4 marker

Once K1000 has brought an engine in, it parks the unused **Aux4** fader (no Volume filter listens to
it: inaudible) at `control.marker_db(guards)` = −61.8 dB + 0.2 dB × (1 subs + 2 tops + 4 Main, for
each fader K1000 holds in a guard or ramp right now). Plain −61.8 dB = brought in, nothing in
flight. The bit is set **before** a guard's mute and cleared **after** its unmute.

| Engine at connect | Read as | k1000 does |
|---|---|---|
| no config, or Main muted at −40 dB and no marker | fresh engine | push the state, boot ramp |
| marker with Main's bit | a k1000 stopped inside a guard or ramp | adopt the rest, ramp Main in again |
| Main muted elsewhere, no marker | someone muted it | adopt as muted, no ramp |
| anything else | playing | adopt, never ramp, never drop the level |

A mute without K1000's record is someone's decision and is never released by K1000.

### One state, two surfaces: adoption and the keep-outside-cuts policy

`k1000` polls `GetConfigJson` + `GetFaders` (~1 Hz) and **adopts** the parameters it owns (local
edits not sent yet win), so the screen and `state.json` follow the phone. What it cannot adopt is
pushed back **only in the safe direction**:

| Outside K1000 (CamillaGUI) | K1000 does | Screen (status bar, orange) |
|---|---|---|
| A cut or a mute: Gain `mute` flag, a Gain/Biquad gain or Volume limit below K1000's, an Aux group lowered or muted | **keeps it**, never raises or unmutes it; an Aux group lowered keeps that offset under later balance moves | `<WHAT> (GUI)` / `N CUTS/MUTES (GUI)` |
| A raise above K1000's value (incl. an Aux group raised) | pulls it down in click-safe steps (0.1 dB per Gain step, 0.5 dB Biquad, 1/12 oct freq/Q) | — (`CONFIG DRIFT (SEE LOG)` if the engine still differs after 3 identical re-sends) |
| Structure changed (mixers, pipeline, a filter's type) | restores it: a whole upload keeping the live devices and the outside cuts, Main muted around it; second look, at most once per 30 s | `PIPELINE CHANGED (GUI)` |
| Devices on another type, another ALSA card (resolved via `/proc/asound/<id>` → `cardN`) or another channel count, the profile's card present (e.g. a GUI page loaded while the engine was down Applies its default `hw:0` config) | uploads the profile's whole config (cuts kept), Main muted, then the boot ramp; second look, at most once per 30 s | `RESTORING ENGINE DEVICES` |
| Any other devices difference (`target_level`, `volume_limit`, chunksize…) | nothing while audio runs (it would restart the IO); fixed at the next engine start | `DEVICES CHANGED (GUI)` |

### Mute, setup actions, persistence

- **Mute never waits.** B4 is sent at once, outside the apply worker and its lock. Only the operator,
  a boot ramp or a setup guard unmutes Main, and the last two re-check the operator's mute right
  before. An operator unmute while a setup guard holds Main waits until the engine runs (dropped,
  never sent, during `stop()`). While K1000 itself holds Main and the operator has not muted, B4 long
  means MUTE (`Controller.b2_muted`, named when mute was B2).
- **Setup actions** (Mono L+R, audition, profile) need the SETUP confirm (D-013: chosen with E2, set
  by a push, `Controller.set_choice`) or a muted box; the profile is refused while k1000.yaml pins
  it. Whole upload with Main muted around it (alert `APPLYING SETUP…`); the unmute waits for
  Running; a profile change ends with the boot ramp.
- **Persistence**: `state.json` + `active.yml`, atomic (tmp + fsync + rename), only when the content
  changed; 1 s after the last edit, at most 5 s after the first. **Edits toward silence** (operator
  mute, master lowered) are saved at once, at most every 0.25 s (measured: on disk after 0.05 s).
  `active.yml` is dumped with libyaml (`dspconfig.YAML_DUMPER`, the same text as PyYAML's
  pure-Python dumper) and only when the config changed: MASTER, balance and mute are faders, so
  their saves write `state.json` alone (the 34 ms pure-Python dump in the event loop starved the
  knob worker of the GIL, PERF-3).
  A failed write is retried after 2 / 5 / 10 s and raises `CANNOT SAVE STATE`. The first overwrite
  of `active.yml` keeps `active.yml.prev`; an unreadable `state.json` is kept as `state.json.bad`.

### Start-up choices (`__main__.run`: `load_settings` → `recover_state` → `choose_profile` → `load_profile`)

`recover_state(paths)` (`control.py`) runs before anything writes either file:

| `state.json` | `active.yml` | Start state | Hint (15 s) |
|---|---|---|---|
| a valid JSON object | — | it (clamped, old files upgraded) | — |
| torn / unreadable / not an object | a K1000 config (`title: K1000 <profile>`) | rebuilt from `active.yml` (the tune the engine boots from: system PEQ, trims, delays, polarity); master and balance defaults; **muted** | `STATE REBUILT · CHECK MASTER` |
| missing | a K1000 config | defaults, **muted** | `NO SAVED STATE · CHECK MASTER` |
| missing or torn | none, or not K1000's | defaults (first boot: unmuted, ramps to −20 dB) | — |

A muted start parks a fresh engine at the master and stays muted; an engine already playing is adopted
unchanged, mute included.

`load_settings` + `choose_profile` (`profiles.py`), `/etc/k1000/k1000.yaml`:

| File | Profile run |
|---|---|
| missing | `{}`: built-in defaults; the profile as below |
| parses, `profile:` known | that one, source `settings` = **pinned** (SETUP cannot switch it) |
| parses, no `profile:` | the last running one (`state.json` `profile`), else `dev` |
| `profile:` unknown | alert `SETTINGS: …`; the last running one, else `dev`. Never silently `dev` |
| does not parse / not a mapping / unreadable | alert `SETTINGS: …`; the `profile:` line is still read on its own, so a typo elsewhere keeps the gig pin |

### The UI process (`__main__.py`)

- Screen and knobs are each **retried every 2 s for the whole life of the process** (a half-opened
  knob driver is closed first). Knobs not open, or unhealthy for more than 1 s: alert
  `KNOBS OFFLINE · USE PHONE MUTE / BYPASS` while a phone can mute (phone servers up,
  SETUP › PHONE › Phone on, the AP up), else `KNOBS OFFLINE · SOURCE DOWN / BYPASS` (B4 does
  nothing meanwhile).
- Render on change only, at least every 1 s, and the panel is rewritten every 5 s even if
  unchanged (heals console overdraw). The frame after a knob/button event comes at ≤ 15 fps; any
  other (meters, the SHOW spectrum) at ≤ 10 fps (UI v1: SHOW with a moving spectrum draws ~10
  frames/s). The meter wakes the screen only when the picture changes (0.25 dB steps within
  −60…0 dBFS), the spectrum only when its frame changes (silence wakes nothing).
- SIGTERM: the console stops first; the screen and the knobs (B4) keep running until the
  controller's `stop()` returns; nothing is muted.

## 3. The pipeline (fixed slots — show-time actions are parameter writes or ramped faders only)

Logical channels after the split: **0 = Sub L, 1 = Sub R, 2 = Top L, 3 = Top R.**

| # | Step | Channels | Contents (filter names are the contract) |
|---|---|---|---|
| 1 | Mixer `input` | N→2 | picks the program L/R from the profile's capture channels |
| 2 | Filter | 0,1 | `in_trim` (Gain) · `sys_peq_1`…`sys_peq_8` (Biquad Peaking) · `live_peq_1`…`live_peq_4` (Biquad Peaking) · `live_lowshelf` (Biquad Lowshelf) · `live_highshelf` (Biquad Highshelf) |
| 3 | Mixer `split` | 2→4 | Sub L/R ← L/R (or mono L+R −6 dB each when `mono_subs`), Top L/R ← L/R |
| 4 | Filter | 0,1 | `sub_trim` (Gain 0 dB, carries polarity `inverted`) · `sub_lowpass` (Biquad Lowpass; *off* = 20 kHz) · `sub_delay` (Delay, `delay_unit: ms`, `subsample: false`) · `sub_fader` (Volume, fader **Aux1**, `limit` +6 dB) |
| 5 | Filter | 2,3 | `top_trim` (Gain 0 dB + polarity) · `top_lowcut` (Biquad Highpass, Q 0.707 = 12 dB/oct; *off* = 10 Hz) · `top_delay` (Delay ms, `subsample: false`) · `top_fader` (Volume, fader **Aux2**, `limit` +6 dB) |
| 6 | Filter | 0…3 each | `out_trim_0`…`out_trim_3` (Gain, per physical output calibration) |
| 7 | Mixer `output` | 4→P | profile mapping to physical outputs |

- **Main fader** = master volume + mute (CamillaDSP's built-in control, ramped 100 ms since UI v1, 300 ms in v0;
  `dspconfig.VOLUME_RAMP_MS`, also the Aux1/Aux2 Volume filters' `ramp_time_ms`), capped at
  0 dB by `devices.volume_limit`.
- **Balance** macro `b` dB: Aux1 (subs) = +b/2, Aux2 (tops) = −b/2.
- **Delays are whole samples**: K1000 writes `delay_samples × 1/48 ms` (6 digits) and never sends a
  delay write that does not change the sample count (each one rebuilds the line and clicks).
- **Delay and polarity are written under a group guard**: the Aux group is muted (marker bit set), the
  value written 0.35 s later, then unmuted. The guard waits for the knob to pause (0.15 s, at most
  0.6 s), so a fast turn is one dip, not one per detent.
- Every other show-time write is click-safe: gains in 0.5 dB steps (0.1 dB on Gain filters, which the
  engine applies as a step), frequency and Q in 1/12-octave steps, steps 12 ms apart.
- Mixer-changing settings (`mono_subs`, profile, `dev` audition) are **setup actions**: they rebuild
  the pipeline (possible click), so the UI only allows them from the SETUP page, with a confirm
  (D-013: E2 chooses, a push sets; the prompt says `· MAY CLICK`).
- "Off" for Lo-Cut / sub low-pass is a frequency move, never a pipeline change.

### Profiles

| Profile | Capture | Program L/R from | Playback | Output mapping (logical → physical) |
|---|---|---|---|---|
| `dev` | `hw:sndrpihifiberry` 2 ch S32_LE | ch 0, 1 | `hw:sndrpihifiberry` 2 ch S32_LE | audition `sum` (out L = SubL + TopL at −6 dB each, R likewise) · `tops` · `subs` |
| `m4` | `hw:M4`, channels **probed** (absent → assumed 4; engine holding it → `/proc/asound/M4/stream0`; free → `--dump-hw-params` with S16 mono, which a 4-channel device refuses: nothing plays); format unset (CamillaDSP picks, S32_LE first) | line inputs 3–4 = ch 2, 3 (default; configurable) | `hw:M4` 4 ch | out1 = Top L, out2 = Top R, out3 = Sub L, out4 = Sub R (configurable) |

Samplerate 48 000, chunksize 256, `target_level` **256 by default** (single clock, no rate adjust),
overridable per profile (`profiles.<name>.target_level`, 64…1536, clamped with a warning; a devices
change, so it applies at the next engine start only). The title `K1000 <profile>` marks K1000's
config. The `m4` profile is **pinned** for gigs: if `hw:M4` is missing, the screen says so — no
silent fallback. An `input_map` outside the capture channels falls back to the profile's own default
when it fits (m4: [2, 3]), else (0, 1); any input/output-map warning raises the alert
`k1000.yaml ROUTING FIXED UP (SEE LOG)`.

## 4. Python package `box/k1000/` (contract)

| Module | Responsibility | Public surface |
|---|---|---|
| `state.py` | the persistent model + ranges | `PeqBand`, `ShelfBand`, `State` dataclasses; `State.to_dict()/from_dict()`; `RANGES` (min, max, step, fine, log?) per field; `delay_samples`; `load_state(path)` (keeps a `.bad` copy) / `save_state_atomic(path, state)` / `atomic_write` (tmp + fsync + rename) |
| `profiles.py` | device profiles and settings | `Profile` (name, devices, channels, format, input map, output map, `target_level`, `warnings`); `load_settings(path)`; `choose_profile(settings, state_profile) -> ProfileChoice(name, source, error)`; `load_profile(name, settings)`; `probe_alsa(device)`; `card_id` / `card_present` (`/proc` only) |
| `dspconfig.py` | CamillaDSP v5 YAML/JSON from profile + state | `generate(profile, state) -> dict`; `to_yaml(cfg)`; `diff_ops` → `SetValue` / `Fader` / `FaderMute` / `Rebuild`; `smooth_ops` (click-safe plans); `boot_ramp_plan`; `state_from_config` (adopt); `config_drift -> Drift(devices, rebuild, fixes)`; `fader_drift`; `with_live_devices` |
| `cdsp.py` | asyncio websocket client (v5) | `CamillaClient(url)`: `connect()`, `command(name, **fields)` (raises `CdspError`), `set_value`, `patch_config`, `set_config_yaml`, `set_fader`, `adjust_fader`, `set_fader_mute`, `get_faders`, `get_config_json`, `get_state`, `get_version`, `get_signal_levels`, `get_stop_reason`…; `on_connect` / `on_disconnect` hooks; auto-reconnect with backoff; serialised request/response |
| `control.py` | the brain | `Controller(client, profile, state, paths, settings=, profile_choice=, boot_gain_db=, boot_ramp_s=, transform=, device_probe=)`: UI actions (`nudge` / `set_value` with a `source`, `toggle`, `reset_live` / `undo_reset` / `undo_left`, `notch_helper`, `set_lowcut`, `set_mute`, `next_page` / `goto_page`, `handle_event`, `b2_muted`, `set_knobs_ok`, `pills`, `alerts`, `set_system_eq`…), apply worker, persistence, boot ramp, adoption and drift policy (§2), device repair (`wrong_devices`), `Status` for the screen (engine, device_present, load, levels, clip, in_clip, muted, ramping, ready, alert, message, drift, knobs_ok); `recover_state(paths)`, `Paths`, `EngineSettings`, `marker_db` / `decode_marker` |
| `hw/mcp23017.py` | knobs + buttons | `ZynthianControls(bus=1, addr=0x20, int_a=27, int_b=4)`; async event stream of `EncoderEvent(index, delta, ts)` and `SwitchEvent(index, kind)` with kinds `press`, `release`, `short`, `long` (fired while still held, at 800 ms); full-quadrature decoding (a click counts on landing, never on a timer; skips credited only to a knob already turning; a click from stillness faster than 2.5 ms per transition is a glitch: docs/08 §9–10), interrupt-driven via gpiod v2 edge events + periodic resync; an INT re-asserted with new levels is a busy knob, not a storm. `hw/simchip.py`: a time-driven MCP23017 + INT lines for timing tests and the knobtrace bench |
| `ui/display.py` | framebuffer | `Framebuffer('/dev/fb0')`: `show(PIL.Image)` (only the span of changed rows is packed to RGB565 little-endian and written; skipped when unchanged), `invalidate()` (next frame written whole), `unbind_console()`, `backlight(on)` |
| `ui/pages.py` | screens | `render(page, state, status, focus) -> PIL.Image` (480×320) for `SHOW`, `ALIGN`, `SETUP`, `HWTEST` (`PHONE` via `ui/qr.py`; v0 `MAIN`/`LIVE` = `ALIGN`/`SHOW`). SETUP (D-013): a header row per group, `<GROUP> <pos>/<size>`, a scroll bar, the selected row red with `now → chosen` while a choice waits, a bottom line saying what the knobs do, read-only rows dimmed. UI v1 (D-009): SHOW draws the live-EQ curve (`ui/eqgraph.py`) over `status.spectrum`; system alerts (`status.alerts`) take the status bar (KNOBS OFFLINE red, full width; warnings orange; states blue), hints the amber strip; `status.pills` on SHOW. NO DEVICE > ENGINE DOWN replace the body; MUTED = red pill + frame + band (SHOW/ALIGN), footer (SETUP) or tag (HWTEST), carrying the alert; CLIP / IN CLIP pills; amber `ENGINE ?` / `DEVICE ?` when unknown; a `UI ERROR` frame instead of a crash; `demo_cases()` → `docs/ui/*.png`; `--bench` = SHOW's cost at 10 fps |
| `ui/input_map.py` | knob/button semantics per page | `map_event(page, focus, event, cfg, muted=, mute_seq=, state=) -> list[Action]`; `KNOBS` table; buttons B1 PAGE · B2 BAND/UNLOCK · B3 NOTCH · B4 MUTE, `B1_TAP` / `B1_HOLD` (D-013); `SETUP_GROUPS` → `SETUP_ITEMS` (kinds num / bool / choice / page / action), `SetChoice`, `SelectGroup`, `Focus.pick` and `Focus.readonly` (D-013); `ControlMap.from_cfg` (k1000.yaml `controls:` encoders / pushes / buttons / reverse; anything invalid → defaults); `KNOB_CORNERS` (confirmed on the box); `knob_tag_style` (k1000.yaml `ui.knob_tags`: dim / off / bright, D-011) |
| `rta.py` | the SHOW spectrum (D-009, §8) | `SpectrumTask(url, wanted=, channels=, publish=, client_factory=)`: its **own** `CamillaClient` (0.5 s command timeout), polls `GetSpectrum` at 10 Hz while `wanted()` (`Controller.spectrum_wanted`), publishes `eqgraph.RtaFrame`s to `Controller.set_spectrum`; `note_input()`; `Ballistics` (fall, slow range tracker, PEAK detector with its level floor and program gate); `peak_floors(settings)`, `spectrum_request`, `parse_spectrum`, `program_level` |
| `laptop.py` | SETUP › PHONE › Laptop access, k1000's side (docs/06 §11) | `LaptopAccess`: `open()` / `close()` / `toggle()` (a flag in `/run/k1000`; root's `k1000-laptop.service` opens the fence), `poll()` (root's `/run/k1000-laptop/status`), `left_s()`, `pill()`; wired by `Controller.attach_laptop` |
| `tune.py` | the `/tune` page's import (docs/06 §11) | `parse_filters(text) -> Import` (REW / EqualizerAPO / typed lines: peaking only, ≤ 8, clamped, skipped and notes listed); `plan_bands`, `curve`; `TuneDesk(ctl)`: `preview`, `apply` / `undo` through `Controller.set_system_eq` |
| `__main__.py` | wiring | asyncio main; GIL switch interval 1 ms before any thread (`configure_gil`: the knob worker needs the GIL 3–4 times per read, PERF-1); systemd-friendly logging; retries (§2); `--stdin` bench console (incl. `ev enc` / `ev sw` real knob events), `--headless`, `--no-hw`, `--test-devices`; never mutes on exit |

### Knob semantics (UI v1, D-009, as built 2026-10-13; buttons and SETUP D-013; knob roles D-015; v0 in git history)

| Page | E1 | E2 | E3 | E4 |
|---|---|---|---|---|
| SHOW (front) | master (0.5 dB; ×4 down, never accelerated up; master-up guard: at most +6 dB per 2 s, `controls.master_up_guard_db`) | frequency of the selected band (1/12 oct, push = 1/48; ×4) | Q (1/6 oct log steps, push = 1/24; shelves: slope) | gain of the selected band (0.5 dB, never accelerated) |
| ALIGN | master (as on SHOW) | DELAY (D-011): `align_delay_ms` = `sub_delay_ms − top_delay_ms`, −50 … +50 ms, 0.1 ms, push = 1 sample, never accelerated; a move writes the late side and zeroes the other, and stops on 0 before crossing; **locked**; unlocked, the first detent after 2.5 s at rest only wakes it | balance (0.5 dB) | Lo-Cut freq (D-011: 2 Hz on a 2 Hz grid, push = 1 Hz; an off-grid value snaps on its first move, never at load; ×4): **locked** |
| SETUP (D-013) | item select (headers skipped, clamps) | value: numbers turn (push = fine); ON/off and choices are chosen, one choice per event, and set by an E1/E2 push | group: the next group's first item; back = this group's first item first (clamps) | — |

Acceleration (input_map): ×1 below 20 detents/s, the knob's cap from 40, the rate taken from the
slower of the last two intervals; a direction change or a 0.25 s pause resets it.

Buttons (D-013: B1 PAGE · B2 BAND/UNLOCK · B3 NOTCH · B4 MUTE): B1 **tap** (on release) = SHOW ↔
ALIGN, SETUP → SHOW, PHONE → SETUP; **hold** = SETUP from SHOW or ALIGN, SHOW from SETUP or PHONE;
the press itself does nothing. ALIGN and SETUP return to SHOW after 60 s without input (not while a
SETUP choice or confirm waits; PHONE and HWTEST never). B2: on SHOW a tap selects the next band, LS
· 1 · 2 · 3 · 4 · HS (wraps, no previous); on ALIGN a hold unlocks E2/E4 for 30 s (renewed by input
on ALIGN, so any 30 s idle relocks), a tap relocks, leaving ALIGN relocks; elsewhere a hint. B3: on
SHOW a tap is the **notch helper** (`Controller.notch_helper`: a free bell at 0 dB, the selected one
first, gets Q 8.65 and the spectrum PEAK's frequency when there is one; a PEAK within 1/6 oct of a
bell in use selects that bell and writes nothing; the gain is never touched; GAIN gets the edit
highlight and the hint `P3 · 2.44k · E4 TO CUT`, or `NO FREE BAND`); a B3 hold only says the reset
is in SETUP; elsewhere a hint. B4 **mute** (long press, every page including PHONE and HWTEST; asks
for the opposite of the mute the screen showed at the press; a short press shows a hint). Encoder
pushes: fine mode while held; on SETUP an E1/E2 push released **without turning** sets the value E2
chose (`SetChoice`; with nothing chosen it proposes the next value), opens a page or runs an action;
Reset live EQ and opening Laptop access need a second push within 5 s. Rows the Controller holds
read-only (`Controller.setup_readonly`: a pinned profile, Audition off the dev profile, a disabled
spectrum) only show why. SETUP › LIVE EQ › Undo reset (one push) restores the live EQ from before
the last reset until the next audible live EQ edit (a gain off 0 dB; inaudible freq/Q moves of a
flat band keep it) or 5 min; an edit that ends it shows `RESET UNDO LOST`. HWTEST: a long press on
any switch but B4 leaves the test.

Screen: system alerts take the status bar in their level's colour (critical red, KNOBS OFFLINE
full width; warning orange; info blue; the page pill shrinks to three letters and the text is
ellipsized below 12 px, never cut); a critical alert is repeated over SHOW's chip row when no
hint is up. Hints are amber and brief. SHOW's persistent pills (`Controller.pills`, LAPTOP OPEN
first) are orange when they change the sound or open the box (LAPTOP OPEN, LO-CUT OFF, POL INV)
and grey otherwise (MONO SUB, AUDITION); below 13 px the neutral ones fold into `+N` first.
Every master change is logged at INFO with its source (`master -19.0 -> -20.0 dB (knob)`;
knob, phone, console, adoption).

## 5. Files on the box

| Path | What |
|---|---|
| `/opt/k1000/` | a mirror of `box/` (package, tests, tools, units, README); `REVISION` says what is deployed |
| `/opt/k1000.prev/` | the version before the last update (rollback) |
| `/etc/k1000/k1000.yaml` | profile pin (`m4` for gigs), `controls:`, `engine:`, `paths:`, per-profile overrides. Never overwritten by install.sh |
| `/var/lib/k1000/state.json` (+ `.bad`) | persistent state (atomic writes); `.bad` = an unreadable copy kept for post-mortem |
| `/var/lib/k1000/active.yml` (+ `.prev`) | generated CamillaDSP config (atomic writes); `.prev` = what the engine booted from before this k1000's first overwrite |
| `/var/log/k1000/` | `camilladsp_rCURRENT.log` + up to 50 rotated (every engine start and every 256 kB), `soak.log` |
| `/etc/systemd/system/{camilladsp,k1000,camillagui}.service` | units from `box/systemd/` |
| `/etc/systemd/system.conf.d/50-k1000-watchdog.conf` | `RuntimeWatchdogSec=15` |
| `/etc/systemd/journald.conf.d/50-k1000-journal.conf` | persistent journal, 32 MB cap (from the next reboot) |

## 6. Rules for the build

- **Never** change the pipeline at show time; never let a crash or restart produce a loud transient
  (boot muted, ramp in, clamp every value to `RANGES`).
- **Never move a fader into an engine that is not Running** (it lands as a step on resume): ramps and
  guard unmutes wait for Running.
- **The operator's mute is never undone by K1000; a cut or mute made outside K1000 is never raised or
  released by K1000.** Only what K1000 itself muted (recorded on the marker) is released.
- Writes that click (delay, polarity) go under a group guard; everything else in click-safe steps.
- All hardware access is wrapped so unit tests run without hardware; hardware tests are separate
  and opt-in (`-m hw`). A missing screen or knob board never stops the service: retried.
- The `dev` box is shared: only the integration step opens `hw:sndrpihifiberry`; module tests use
  CamillaDSP's `SignalGenerator` → `File` (`/dev/null`) devices on their own websocket port
  (`K1000_CDSP_TEST_PORT`, `K1000_DSPCONFIG_PORT`, `K1000_CONTROL_TEST_PORT`, `K1000_SYS_TEST_PORT`).

## 7. What the build changed (2026-10-12)

| Topic | Morning contract | As built | Why |
|---|---|---|---|
| Engine flags | `camilladsp -m -g -40 …` | `-g-40`; the unit waits for a valid `active.yml` and the device, rotates logs (256 kB × 50); no `-w` | `-g -40` is an unknown option; an absent M4 caused a restart and a log rotation every second |
| Delay | `delay_unit: ms`, push = 0.01 ms | whole samples, push = 1 sample (1/48 ms) | `subsample: false` rounds; a write that keeps the count still clicks |
| Boot | set −40, unmute, ramp | gated on Running; `NO AUDIO FLOWING · HELD MUTED`; re-park if it stops; a saved mute is kept | a fader move into a stalled engine lands as a step |
| Restart detection | implicit | Aux4 marker `marker_db(guards)` | tell a fresh engine, a k1000 killed mid-guard and someone's mute apart |
| Adoption | adopt external changes | adopt owned values; keep outside cuts and mutes; pull raises down; restore the pipeline; repair wrong devices | the phone must never make K1000 raise a level or unmute |
| Torn `state.json` | safe defaults | `recover_state` from `active.yml`, muted | defaults would overwrite the tune on disk and in the engine |
| Profile choice | `m4` pinned | `choose_profile` (settings > last running > `dev`; errors shown, the pin survives a typo) | a bad k1000.yaml must not silently run `dev` |
| `target_level` | 256 fixed | 256 default, per-profile override | USB margin on the M4 (m4-2), decided by Thursday's soak |
| Clip | output peaks | + `IN CLIP` (capture peaks of the program channels ≥ −0.5 dBFS, held 3 s) and a `CLIP` pill | the M4 line input clips first; no K1000 knob fixes it |
| Buttons | B3 and B4 on every page, one long B4 | B3 MAIN only; B4 LIVE only, two long presses; E3 arm; SETUP select only without turning | B2 and B4 are the same gesture; brushed knobs |
| Screen | overlays MUTED, NO DEVICE, ENGINE DOWN | + alerts in the MUTED band, READY only when Running and in sync, `RAMPS IN BY ITSELF`, drift notes, CLIP pills | the band must never say something false about what happens next |
| Persistence | debounced | + immediate saves toward silence, retries, `.prev` / `.bad` | a power cut right after a mute must not boot loud |
| UI process | open screen and knobs at start | retried for life; `KNOBS OFFLINE`; render on change (10.1 % → 1.6 % of a core, in-process); forced rewrite every 5 s | boot race, I²C glitches, CPU |

**Still open** (owners in `ROADMAP.md`): knob/button positions and `KNOB_CORNERS` (Knob test) ·
fbcon vs the `ExecStartPre` unbind at boot (ops-1; keep cloud-init until settled) · the B1 page cycle
and sub delay on MAIN (Thomas) · boot-muted policy at power-on (Thomas; today: the saved mute) ·
pinning the M4 channel counts after Tuesday's probe · smbus → smbus2 (D-008) · the D-007 phone AP/PWA
(not built).

## 8. UI v1 (D-009, as built 2026-10-13)

After using the v0 panel, Thomas decided **D-009**, the contract for this build. The design is
`07-live-ui-v1.md` (where it differs from D-009, D-009 won; its "As built" note lists what the build
did differently), the knob work is `08-knob-precision.md` (patches A and B), and the operator view
is `04-operator-card.md`. Accepted on the box on 2026-10-13 (`f7aa58b-v1`: the full suite, a drill on
the real panel, a 15-minute soak with the spectrum on, a power cycle). It settles two of §7's open
items: the B1 page cycle, and sub delay off the front page.

| Topic | v0 | UI v1 |
|---|---|---|
| Pages | MAIN → LIVE → SETUP (B1) | **SHOW** (front: master, the selected band's freq/gain/Q, the live-EQ curve over the program spectrum) → **ALIGN** (master and balance live; the delay (D-011: one signed sub/top delay) and Lo-Cut **locked**, a B3 hold unlocks them for 30 s of idle) → **SETUP**. B1 hold = SHOW; ALIGN and SETUP return to SHOW after 60 s without input (`Controller.ui_tick`). The v0 names stay aliases (`input_map.PAGE_ALIASES`). D-013: a B1 tap flips SHOW ↔ ALIGN, a B1 hold opens SETUP; the unlock is a B2 hold |
| Buttons | B3 Lo-Cut on/off (MAIN); B4 reset live EQ, two long presses (LIVE) | B3: next band on SHOW, unlock on ALIGN. B4: the notch helper (`Controller.notch_helper`, never writes a gain). Reset live EQ (with a one-level `undo_reset`, kept 300 s or until the next audible live-EQ edit) and Lo-Cut on/off are SETUP items with a confirm, the same `reset_live` / `set_lowcut` paths the phone uses. D-013: band and unlock on B2, the notch helper on B3, mute on B4; Lo-Cut on/off is chosen with E2 and set by a push |
| Fader ramp | 300 ms | **100 ms** (`dspconfig.VOLUME_RAMP_MS`): Main's `devices.volume_ramp_time_ms` and the Aux1/Aux2 Volume filters' `ramp_time_ms`. Mute and unmute fade over 100 ms, the delay-guard dip lasts 0.15 s, the boot ramp takes ~30 steps over 3 s. Main's ramp is a `devices` value, which K1000 never forces while audio runs: a deploy of this change restarts camilladsp |
| Master up | accelerated ×2 | never accelerated, and **at most `controls.master_up_guard_db` (6 dB) within any 2 s**, counted over every `Controller.nudge` (knob, phone, console); further up-detents are refused with the hint `MASTER: SLOW DOWN` and a log line; 0 = off. Down is never limited and may accelerate ×4. The phone keeps its own rate limit on top (`06-phone.md`) |
| Knob decoder | pause rule on a timer; acceleration from 12 det/s | patch A: a click counts on landing, never on a timer, so a resting knob emits nothing; `steps_per_detent` / `detent_ab` per encoder in `controls:`. Patch B: ×1 below 20 det/s (the slower of the last two intervals), the knob's cap from 40 det/s |
| Spectrum | — | `rta.SpectrumTask` on its **own** engine connection (below) |
| Screen | alerts in the MUTED band or the middle strip | system alerts in the status bar (critical red, KNOBS OFFLINE full width; warning orange; info blue); hints in the amber strip; persistent pills on SHOW (`Controller.pills`) |
| Laptop | wired LAN only | SETUP › PHONE › Laptop access and the `/tune` page (`06-phone.md` §11; root helper `k1000-laptop.service`) |
| `state.json` | — | + `spectrum_on` (v0 ignores unknown keys, so a rollback still loads it) |

**The spectrum task (`rta.py`)**

- **Its own connection.** `CamillaClient` serialises every command behind one lock, and a config
  upload holds that lock until its read-back, so a spectrum poll must never queue in front of a
  mute. The task owns a second client (0.5 s command timeout, ping every 5 s, reconnect backoff at
  most 2 s). When that link stalls, mute, faders and adoption do not notice. It is closed after 30 s
  of not being wanted.
- **When.** At 10 Hz, only while `Controller.spectrum_wanted()`: SHOW displayed, SETUP › LIVE EQ › Spectrum ON,
  engine Running or Paused, device present. With `spectrum: enabled: false` in k1000.yaml there is no
  task and no second connection. Not wanted for less than `rta.KEEP_S` (1.5 s): no poll, but the
  last frame and the PEAK are kept, so a quick look at ALIGN and back (two B1 taps, D-013) followed
  by B3 still finds the PEAK; not wanted longer, the frame is cleared (no stale picture later).
- **What.** `{"command":"GetSpectrum","value":{"side":"capture","channel":N,"min_freq":10,"max_freq":20000,"n_bins":264}}`
  (1/24 oct) for each of the profile's `input_map` channels (m4 2, 3; dev 0, 1), combined as a power
  average. Never `channel: null`: on the M4 it would average the mic and the spare input into the
  program. Then `{"command":"GetCaptureSignalRmsSince","value":0.5}` on the same connection: the
  program's level for the PEAK's program gate.
- **Errors.** A fresh engine first answers "No audio data available" (the call starts its
  analyser), then "Insufficient data in buffer" until one FFT is buffered: the analyser warming up,
  retried quietly every 0.4 s (state `waiting`: no picture, no `NO SPECTRUM`, nothing logged). Still
  warming up 10 s after its first such answer, it counts as a refusal ("No audio data" again after
  another answer is an engine restart and starts the 10 s again, but 30 s of warm-up answers on one
  link without data is a refusal whatever). Any other refusal is logged
  once, sets the state `unavailable` (the grey `NO SPECTRUM` tag on SHOW) and is retried every 5 s.
  No data for 1 s clears the frame: never a frozen picture.
- **Ballistics** (`rta.Ballistics`): bars rise at once and fall at 24 dB/s, with 3 s hold dots. The
  display range follows the loudest bin as a slow tracker (rises at once, falls 1 dB/s, no 6 dB
  steps). PEAK = a bin 10 dB above the median of its ±1 octave (released 0.5 s after it drops below
  6 dB), within 30 dB of the loudest bin, between 40 Hz and 16 kHz, persistent for 1.5 s, judged on a
  0.25 s power average; the pointer moves only when the peak moves by 2 bins. **Never on silence**:
  the peak bin must reach `spectrum.peak_floor_db` (default −80 dBFS on the engine's peak-bin scale,
  0 = a full-scale sine; kept down to −84 once shown) and the program must be present: the
  `input_map` channels' capture RMS over 0.5 s at `spectrum.program_floor_db` or above (default −60
  dBFS, kept down to −63). When the program stops, the PEAK goes 0.5 s after its gate closes, and B3
  (the notch helper, D-013) is its `NO PEAK` case. Scales measured on the box (a private engine's SignalGenerator): white noise
  at −25 dBFS RMS reads −56…−50 dBFS per bin (63 Hz…16 kHz), at −45 RMS −76…−70; a −20 dBFS sine
  peaks at −20.6. The bench's silent HiFiBerry input reads −95…−108 dBFS RMS, its noise-floor tone
  about −114. An engine that refuses the RMS command leaves the level floor alone in charge.
- **The hand first.** Every knob or button event holds new frames for 300 ms (`note_input`).
  `Controller.set_spectrum` wakes the screen only when the frame changes, and never bumps `version`
  (phones do not show the spectrum). Spectrum frames come at ≤ 10 fps, the frame after a knob event
  at ≤ 15 fps (§2).
- **Cost on the box** (silent HiFiBerry input, 60 s on SHOW): k1000 19.6 % of one core (max 32.8 %),
  camilladsp 4.7 %, engine processing load 0.98 % (max 1.86 %), 0 xruns. Measured at the
  acceptance, before the program gate added its one small RMS call per poll. Re-measured with the
  gate over the 1 h 34 min soak on 2026-10-13 (SHOW + spectrum, silent input, systemd CPU
  counters): k1000 **25.5 %** of one core on average, camilladsp 6.0 %, 0 xruns, SoC max 55 °C.

**Found at the acceptance (2026-10-13), fixed after it** (k1000 code only: deploy with
`install.sh` without `--start`, then `sudo systemctl restart k1000`; the engine keeps playing):

- *`NO SPECTRUM` and a WARNING for ~5 s after every engine start* (seen at the deploy and at boot,
  9.7–14.6 s): CamillaDSP 5.0.0-beta.1's `InvalidRequestError: Insufficient data in buffer` counted
  as a refusal. It is now a warm-up answer, retried quietly every 0.4 s (Errors, above).
- *PEAK on a silent input*: a noise-floor bin (~7.3 kHz, about −114 dBFS) showed as PEAK and B4 aimed
  at it. The level floor and the program gate (Ballistics, above) now keep the PEAK off silence and
  off the gaps between tracks.
