# 03 — v0 architecture (the build contract, 2026-10-12)

The contract every v0 module is built against. Scope and knob plan: `02-plan-to-sunday.md`.
Decisions: D-002 (Zynthian V4 + CamillaDSP), D-004 (CamillaDSP **v5.0.0-beta.1**), D-005 (Pi OS).

## 1. Proven hardware facts (on the box, 2026-10-12)

| Item | Fact |
|---|---|
| Board | Raspberry Pi 4 Model B Rev 1.4, 8 GB; Raspberry Pi OS **trixie** 64-bit, kernel 6.18, Python **3.13** |
| Audio (`dev` profile) | HiFiBerry DAC+ADC Pro, auto-loaded from its HAT EEPROM. ALSA card **`sndrpihifiberry`** (`hw:sndrpihifiberry`). Playback 2 ch S16/S24/S32_LE, capture 2 ch S16/S32_LE, 48 kHz OK |
| Audio (`m4` profile) | MOTU M4 (arrives 2026-10-13): ALSA card expected **`M4`**; channel counts and format to be **probed**, not assumed |
| Screen | ZynScreen 3.5″ = ILI9486 via `fb_ili9486` → **`/dev/fb0`**, 480×320, **RGB565** (16 bpp, stride 960). Overlay `box/overlays/k1000-zynscreen.dts`. Backlight lit when GPIO22 is HIGH. Linux console (`vtcon1`) is bound to it — the UI must unbind it (`echo 0 > /sys/class/vtconsole/vtcon1/bind`, root) |
| Touch | ADS7846 on SPI0 CE1 → input device "ADS7846 Touchscreen" (not used in v0) |
| Knobs | MCP23017 at **I²C bus 1, 0x20**. Pull-ups must be enabled (GPPUA/GPPUB = 0xFF). **INTA → BCM GPIO27, INTB → BCM GPIO4**, active-low (proven by forced interrupts) |
| Encoders (A, B pins) | enc0 = GPA2, GPA1 · enc1 = GPA5, GPA4 · enc2 = GPB2, GPB1 · enc3 = GPB5, GPB4 |
| Switches (active-low) | sw0..3 = encoder pushes GPA0, GPA3, GPB0, GPB3 · sw4..7 = buttons GPA6, GPA7, GPB6, GPB7. **Physical positions unknown** → mapping is config, and a hw-test mode identifies them |
| Engine | CamillaDSP **5.0.0-beta.1** at `/usr/local/bin/camilladsp`; websocket v5 protocol proven (`{"command": X, ...}` → `{"reply": X, "result": "Ok", "value": ...}`); load ~0.1 % for a 2-ch generator |
| Python deps (apt) | python3-websockets 15, python3-smbus, python3-libgpiod (gpiod **2.2** API), python3-yaml, python3-pil, python3-numpy, python3-pytest |

Reference docs (v5, downloaded): CamillaDSP `README.md`, `websocket.md`, `backend_alsa.md`, `CHANGELOG.md`.

## 2. Process model

```
camilladsp.service ── camilladsp -m -g -40 -p 1234 -a 127.0.0.1 /var/lib/k1000/active.yml
       ▲  websocket 127.0.0.1:1234                    (boots MUTED from the last generated config)
       │
k1000.service ── python3 -m k1000   (the control service: state, knobs, screen, persistence)
       │  writes /var/lib/k1000/state.json + /var/lib/k1000/active.yml (atomic, debounced)
       │
camillagui.service ── CamillaGUI v5 beta (phone/laptop UI), talks to the same websocket
```

- **Audio never depends on the UI.** If `k1000` dies, CamillaDSP keeps playing as it was. If CamillaDSP
  (re)starts, it comes up **muted** (`-m`) at a safe gain; `k1000` detects it, re-applies, and ramps in.
- **Boot**: camilladsp starts muted from `active.yml` (state baked in) → `k1000` connects → verifies
  config → sets Main to −40 dB, unmutes, ramps Main to the saved master over ~3 s.
- **One state, two surfaces**: CamillaGUI edits the same config. `k1000` polls `GetConfigJson` +
  `GetFaders` (~1 Hz) and **adopts** external changes to the parameters it owns, so the screen and
  the saved state follow the phone.

## 3. The pipeline (fixed slots — show-time actions are parameter writes or ramped faders only)

Logical channels after the split: **0 = Sub L, 1 = Sub R, 2 = Top L, 3 = Top R.**

| # | Step | Channels | Contents (filter names are the contract) |
|---|---|---|---|
| 1 | Mixer `input` | N→2 | picks the program L/R from the profile's capture channels |
| 2 | Filter | 0,1 | `in_trim` (Gain) · `sys_peq_1`…`sys_peq_8` (Biquad Peaking) · `live_peq_1`…`live_peq_4` (Biquad Peaking) · `live_lowshelf` (Biquad Lowshelf) · `live_highshelf` (Biquad Highshelf) |
| 3 | Mixer `split` | 2→4 | Sub L/R ← L/R (or mono L+R −6 dB each when `mono_subs`), Top L/R ← L/R |
| 4 | Filter | 0,1 | `sub_trim` (Gain, carries polarity `inverted`) · `sub_lowpass` (Biquad Lowpass; *off* = 20 kHz) · `sub_delay` (Delay, `delay_unit: ms`) · `sub_fader` (Volume, fader **Aux1**) |
| 5 | Filter | 2,3 | `top_trim` (Gain + polarity) · `top_lowcut` (Biquad Highpass, Q 0.707 = 12 dB/oct; *off* = 10 Hz) · `top_delay` (Delay ms) · `top_fader` (Volume, fader **Aux2**) |
| 6 | Filter | 0…3 each | `out_trim_0`…`out_trim_3` (Gain, per physical output calibration) |
| 7 | Mixer `output` | 4→P | profile mapping to physical outputs |

- **Main fader** = master volume + mute (CamillaDSP's built-in control, ramped).
- **Balance** macro `b` dB: Aux1 (subs) = +b/2, Aux2 (tops) = −b/2.
- Mixer-changing settings (`mono_subs`, profile, `dev` audition) are **setup actions**: they rebuild
  the pipeline (possible click), so the UI only allows them from the SETUP page, with a confirm.
- "Off" for Lo-Cut / sub low-pass is a frequency move, never a pipeline change.

### Profiles

| Profile | Capture | Program L/R from | Playback | Output mapping (logical → physical) |
|---|---|---|---|---|
| `dev` | `hw:sndrpihifiberry` 2 ch S32_LE | ch 0, 1 | `hw:sndrpihifiberry` 2 ch S32_LE | audition `sum` (out L = SubL + TopL at −6 dB each, R likewise) · `tops` · `subs` |
| `m4` | `hw:M4`, channels/format **probed** | line inputs 3–4 (default; configurable) | `hw:M4` 4 ch | out1 = Top L, out2 = Top R, out3 = Sub L, out4 = Sub R (configurable) |

Samplerate 48 000, chunksize 256, `target_level` 256 (single clock, no rate adjust),
`volume_ramp_time_ms` 300. The `m4` profile is **pinned** for gigs: if `hw:M4` is missing, the screen
says so — no silent fallback.

## 4. Python package `box/k1000/` (contract)

| Module | Responsibility | Public surface |
|---|---|---|
| `state.py` | the persistent model + ranges | `PeqBand`, `ShelfBand`, `State` dataclasses; `State.to_dict()/from_dict()`; `RANGES` (min, max, step, log?) per field; `load_state(path) / save_state_atomic(path, state)` (tmp + fsync + rename) |
| `profiles.py` | device profiles | `Profile` (name, capture/playback device, channels, format, input map, output map); `load_profile(name, overrides)`; `probe_alsa(device)` via `--dump-hw-params` |
| `dspconfig.py` | CamillaDSP v5 YAML/JSON from profile + state | `generate(profile, state) -> dict`; `to_yaml(cfg)`; `diff_ops(old_state, new_state) -> list[Op]` where `Op` = `SetValue(pointer, value)` / `Fader(index, db)` / `FaderMute(index, bool)` / `Rebuild(reason)`; `state_from_config(cfg_json, faders, base_state) -> State` (adopt external edits) |
| `cdsp.py` | asyncio websocket client (v5) | `CamillaClient(url)`: `connect()`, `command(name, **fields) -> value` (raises `CdspError(result, message)`), helpers `set_value`, `set_fader`, `adjust_fader`, `set_fader_mute`, `get_faders`, `get_config_json`, `set_config_yaml`, `get_state`, `get_signal_levels`, `get_stop_reason`; auto-reconnect with backoff; serialised request/response |
| `control.py` | the brain | `Controller(client, profile, state, paths)`: UI actions (`nudge(field, detents, fine)`, `toggle(field)`, `reset_live()`, `set_mute(bool)`, `next_page()`…), applies `diff_ops`, debounced persistence (state.json + active.yml), boot ramp, external-change adoption, status for the screen (engine state, load, levels, clip, device present) |
| `hw/mcp23017.py` | knobs + buttons | `ZynthianControls(bus=1, addr=0x20, int_a=27, int_b=4)`; async event stream of `EncoderEvent(index, delta, ts)` and `SwitchEvent(index, kind)` with kinds `press`, `release`, `short`, `long` (≥ 800 ms); full-quadrature decoding, interrupt-driven via gpiod v2 edge events + periodic resync |
| `ui/display.py` | framebuffer | `Framebuffer('/dev/fb0')`: `show(PIL.Image)` (RGB565 little-endian), `unbind_console()`, `backlight(on)` |
| `ui/pages.py` | screens | `render(page, state, status, focus) -> PIL.Image` (480×320) for pages `MAIN`, `LIVE`, `SETUP`, `HWTEST`, and the overlays `MUTED`, `NO DEVICE`, `ENGINE DOWN`; legible at arm's length (big digits) |
| `ui/input_map.py` | knob/button semantics per page | `map_event(page, focus, event) -> list[Action]`; mapping table loaded from `/etc/k1000/k1000.yaml` (physical → logical) |
| `__main__.py` | wiring | asyncio main; systemd-friendly logging; clean shutdown |

### Knob semantics (v0)

| Page | E1 | E2 | E3 | E4 |
|---|---|---|---|---|
| MAIN | master (0.5 dB) | balance (0.5 dB) | sub delay (0.1 ms, push = 0.01) | Lo-Cut freq (1/12 oct) |
| LIVE | slot select (PEQ 1–4, Low shelf, High shelf) | frequency (1/12 oct, push = 1/48) | gain (0.5 dB) | Q (log steps) |
| SETUP | item select | value | — | — |

Buttons: B1 next page · B2 **mute** (long press; short press shows a hint) · B3 Lo-Cut on/off ·
B4 **reset live EQ** (long press). Encoder pushes: fine mode while held (E2/E3), or "select" on SETUP.
Acceleration on fast turns for master/frequency.

## 5. Files on the box

| Path | What |
|---|---|
| `/opt/k1000/` | deployed `box/k1000` package + `box/tests` (rsync from the repo) |
| `/etc/k1000/k1000.yaml` | profile choice (`m4` pinned for gigs), hw mapping, options |
| `/var/lib/k1000/state.json` | persistent state (atomic writes) |
| `/var/lib/k1000/active.yml` | generated CamillaDSP config (atomic writes) |
| `/etc/systemd/system/{camilladsp,k1000,camillagui}.service` | units from `box/systemd/` |

## 6. Rules for the build

- **Never** change the pipeline at show time; never let a crash or restart produce a loud transient
  (boot muted, ramp in, clamp every value to `RANGES`).
- All hardware access is wrapped so unit tests run without hardware; hardware tests are separate
  and opt-in (`-m hw`).
- The `dev` box is shared: only the integration step opens `hw:sndrpihifiberry`; module tests use
  CamillaDSP's `SignalGenerator` → `File` (`/dev/null`) devices on their own websocket port.
