# 03 — v0 architecture (the build contract, 2026-10-12)

The contract every v0 module is built against. Scope and knob plan: `02-plan-to-sunday.md`.
Decisions: D-002 (Zynthian V4 + CamillaDSP), D-004 (CamillaDSP **v5.0.0-beta.1**), D-005/D-006 (Pi OS Lite).

**Corrected to the build, 2026-10-12 evening** (`e3a3deb` + the fix round of the same day). What the
build proved different is fixed in place below and listed in §7. Operator view:
`04-operator-card.md` · M4 bring-up: `05-m4-day.md` · deploy and ops: `box/README.md`.

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
| Switches (active-low) | sw0..3 = encoder pushes GPA0, GPA3, GPB0, GPB3 · sw4..7 = buttons GPA6, GPA7, GPB6, GPB7. **Physical positions still unknown** → mapping is config (`controls:`), identified with SETUP › Knob test |
| Engine | CamillaDSP **5.0.0-beta.1** at `/usr/local/bin/camilladsp`; websocket v5 protocol (`{"command": X, ...}` → `{"reply": X, "result": "Ok", "value": ...}`) |
| Engine facts measured by the build | A negative gain must touch its flag: **`-g-40`** (`-g -40` is an unknown option). A fresh engine reads Main muted at −40 dB and every Aux fader at 0 dB. A Delay with `subsample: false` is a whole number of samples, rounded to nearest (0.0104 ms → 0, 0.0105 ms → 1), and every Delay write rebuilds the delay line (a click). **Any** `devices` change, even `volume_limit` alone via SetConfigValue, restarts capture and playback. A fader change made while the engine is not Running is applied as a step when audio resumes. Started from a file on an absent device, the engine exits 0 within ~20 ms |
| Python deps (apt) | python3-websockets 15, python3-smbus (GPL-2-only: moving to python3-smbus2, D-008), python3-libgpiod (gpiod **2.2** API), python3-yaml, python3-pil, python3-numpy, python3-pytest |

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
| 5 | **Waits until GetState reads Running**, twice, 12 ms apart (> 2 chunks). No timeout. If it has to wait: alert `NO AUDIO FLOWING · HELD MUTED`. The MUTED band says `RAMPS IN BY ITSELF · hold B2 to keep muted` throughout |
| 6 | Unmutes, then steps Main to the saved master over `boot_ramp_s` (3 s): one step per 300 ms engine ramp, ≤ 6 dB per step, Running re-checked before each. Engine stops mid-ramp → Main muted and re-parked, the ramp restarts once audio flows |
| 7 | Clears Main's marker bit |

- A state saved **muted** parks Main at the master and stays muted (`READY · hold B2 to unmute`).
- The operator wins: B2 during the ramp drops its unmute; a master turned down caps the steps.
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

| Outside K1000 (CamillaGUI) | K1000 does | Screen (middle strip) |
|---|---|---|
| A cut or a mute: Gain `mute` flag, a Gain/Biquad gain or Volume limit below K1000's, an Aux group lowered or muted | **keeps it**, never raises or unmutes it; an Aux group lowered keeps that offset under later balance moves | `<WHAT> OUTSIDE K1000` / `N CUTS/MUTES MADE OUTSIDE K1000` |
| A raise above K1000's value (incl. an Aux group raised) | pulls it down in click-safe steps (0.1 dB per Gain step, 0.5 dB Biquad, 1/12 oct freq/Q) | — (`CONFIG DRIFT (SEE LOG)` if the engine still differs after 3 identical re-sends) |
| Structure changed (mixers, pipeline, a filter's type) | restores it: a whole upload keeping the live devices and the outside cuts, Main muted around it; second look, at most once per 30 s | `PIPELINE CHANGED OUTSIDE K1000` |
| Devices on another type, another ALSA card (resolved via `/proc/asound/<id>` → `cardN`) or another channel count, the profile's card present (e.g. a GUI page loaded while the engine was down Applies its default `hw:0` config) | uploads the profile's whole config (cuts kept), Main muted, then the boot ramp; second look, at most once per 30 s | `RESTORING ENGINE DEVICES` |
| Any other devices difference (`target_level`, `volume_limit`, chunksize…) | nothing while audio runs (it would restart the IO); fixed at the next engine start | `ENGINE DEVICES CHANGED OUTSIDE K1000` |

### Mute, setup actions, persistence

- **Mute never waits.** B2 is sent at once, outside the apply worker and its lock. Only the operator,
  a boot ramp or a setup guard unmutes Main, and the last two re-check the operator's mute right
  before. An operator unmute while a setup guard holds Main waits until the engine runs (dropped,
  never sent, during `stop()`). While K1000 itself holds Main and the operator has not muted, B2 long
  means MUTE (`Controller.b2_muted`).
- **Setup actions** (mono subs, audition, profile) need the SETUP double push or a muted box; the
  profile is refused while k1000.yaml pins it. Whole upload with Main muted around it (alert
  `APPLYING SETUP…`); the unmute waits for Running; a profile change ends with the boot ramp.
- **Persistence**: `state.json` + `active.yml`, atomic (tmp + fsync + rename), only when the content
  changed; 1 s after the last edit, at most 5 s after the first. **Edits toward silence** (operator
  mute, master lowered) are saved at once, at most every 0.25 s (measured: on disk after 0.05 s).
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

- Screen and knobs are each **retried every 2 s for the whole life of the process** (a half-opened knob
  driver is closed first). Knobs not open, or unhealthy for more than 1 s: alert
  `KNOBS OFFLINE · USE PHONE MUTE / BYPASS` (B2 does nothing meanwhile).
- Render on change only (≤ 15 fps), at least every 1 s, and the panel is rewritten every 5 s even if
  unchanged (heals console overdraw). The meter wakes the screen only when the picture changes
  (0.25 dB steps within −60…0 dBFS).
- SIGTERM: the console stops first; the screen and the knobs (B2) keep running until the
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

- **Main fader** = master volume + mute (CamillaDSP's built-in control, ramped 300 ms), capped at
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
  the pipeline (possible click), so the UI only allows them from the SETUP page, with a confirm.
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
| `control.py` | the brain | `Controller(client, profile, state, paths, settings=, profile_choice=, boot_gain_db=, boot_ramp_s=, transform=, device_probe=)`: UI actions (`nudge`, `toggle`, `reset_live`, `set_mute`, `next_page`, `handle_event`, `b2_muted`, `set_knobs_ok`…), apply worker, persistence, boot ramp, adoption and drift policy (§2), device repair (`wrong_devices`), `Status` for the screen (engine, device_present, load, levels, clip, in_clip, muted, ramping, ready, alert, message, drift, knobs_ok); `recover_state(paths)`, `Paths`, `EngineSettings`, `marker_db` / `decode_marker` |
| `hw/mcp23017.py` | knobs + buttons | `ZynthianControls(bus=1, addr=0x20, int_a=27, int_b=4)`; async event stream of `EncoderEvent(index, delta, ts)` and `SwitchEvent(index, kind)` with kinds `press`, `release`, `short`, `long` (fired while still held, at 800 ms); full-quadrature decoding, interrupt-driven via gpiod v2 edge events + periodic resync |
| `ui/display.py` | framebuffer | `Framebuffer('/dev/fb0')`: `show(PIL.Image)` (RGB565 little-endian, skipped when unchanged), `invalidate()`, `unbind_console()`, `backlight(on)` |
| `ui/pages.py` | screens | `render(page, state, status, focus) -> PIL.Image` (480×320) for `MAIN`, `LIVE`, `SETUP`, `HWTEST`. NO DEVICE > ENGINE DOWN replace the body; MUTED = red pill + frame + band (MAIN/LIVE), footer (SETUP) or tag (HWTEST), carrying the alert; CLIP / IN CLIP pills; amber `ENGINE ?` / `DEVICE ?` when unknown; a `UI ERROR` frame instead of a crash; `demo_cases()` → `docs/ui/*.png` |
| `ui/input_map.py` | knob/button semantics per page | `map_event(page, focus, event, cfg, muted=) -> list[Action]`; `KNOBS` table; `ControlMap.from_cfg` (k1000.yaml `controls:` encoders / pushes / buttons / reverse; anything invalid → defaults); `KNOB_CORNERS` (assumed) |
| `__main__.py` | wiring | asyncio main; systemd-friendly logging; retries (§2); `--stdin` bench console (incl. `ev enc` / `ev sw` real knob events), `--headless`, `--no-hw`, `--test-devices`; never mutes on exit |

### Knob semantics (v0, as built)

| Page | E1 | E2 | E3 | E4 |
|---|---|---|---|---|
| MAIN | master (0.5 dB; acceleration ×2 up, ×4 down) | balance (0.5 dB) | sub delay (0.1 ms, push = 1 sample = 1/48 ms; after 2.5 s at rest the first detent only wakes it) | Lo-Cut freq (1/12 oct, push = 1/48; ×4) |
| LIVE | slot select (PEQ 1–4, Low shelf, High shelf; every detent counts, wraps) | frequency (1/12 oct, push = 1/48; ×4) | gain (0.5 dB) | Q (1/6 oct log steps, push = 1/24) |
| SETUP | item select (clamps) | value (numeric items; push = fine) | — | — |

Buttons: B1 next page MAIN → LIVE → SETUP (on press; HWTEST only via SETUP › Knob test) · B2 **mute**
(long press, every page including HWTEST; asks for the opposite of the mute the screen shows; a short
press shows a hint) · B3 Lo-Cut on/off (short press, **MAIN only**; elsewhere a hint) · B4 **reset live
EQ** (**LIVE only**: a long press arms it for 5 s, a second long press resets; elsewhere a hint).
Encoder pushes: fine mode while held; on SETUP an E1/E2 push released **without turning** selects (bool
toggles, Knob test), and rebuild items need a second push within 5 s. HWTEST: a long press on any
switch but B2 leaves the test. No acceleration in fine mode or after a direction change.

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
