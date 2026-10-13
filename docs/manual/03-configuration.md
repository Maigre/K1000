# 3 — Configuration

Everything the box reads at start lives in **`/etc/k1000/k1000.yaml`**. The sound itself (levels,
EQ, delays) is not configuration: it is the **state**, saved by K1000 in `/var/lib/k1000/` as you
work ([§10](#10-the-state-files)).

## 1. The file

- Read **once, when the k1000 service starts**. After any edit:
  `sudo systemctl restart k1000` (between shows: the audio keeps playing, the knobs and the screen
  pause for a few seconds).
- Copied from the repository's `box/etc/k1000.yaml` at the first install, then **never overwritten**
  by `install.sh`.
- **Missing** = built-in defaults, the same values as the template.
- **Lenient.** A bad value never stops the box: it falls back to its default (or is clamped into
  its range). For `profile`, `profiles`, `phone`, `spectrum`, `ui` and the knob keys
  `steps_per_detent` and `detent_ab` the journal says so (`journalctl -u k1000`). The rest of `controls`, `engine`
  and `paths` fall back **silently**: check the knobs on SETUP › Knob test.
  A file that does not parse at all raises the alert `SETTINGS: …` on the screen; every key then
  takes its default, except the `profile:` line, which is still read on its own, so a typo
  elsewhere does not lose the gig pin.
- Edit it with `sudo nano /etc/k1000/k1000.yaml`. YAML: spaces, never tabs.

## 2. A complete example

```yaml
profile: m4                      # the gig pin

profiles:
  dev:
    capture_device: hw:sndrpihifiberry
    playback_device: hw:sndrpihifiberry
    capture_channels: 2
    playback_channels: 2
    capture_format: S32_LE
    playback_format: S32_LE
    input_map: [0, 1]
    probe: false
  m4:
    capture_device: hw:M4
    playback_device: hw:M4
    capture_channels: 4          # pinned after the M4 bring-up (null = probe)
    playback_channels: 4
    capture_format: null
    playback_format: null
    input_map: [2, 3]
    output_map: [TopL, TopR, SubL, SubR]
    probe: true
    target_level: 512            # D-010: margin over USB; DJ/program only

controls:
  encoders: [0, 1, 2, 3]
  pushes: [0, 1, 2, 3]
  buttons: [4, 5, 6, 7]
  reverse: [false, false, false, false]
  steps_per_detent: [4, 4, 4, 4]
  detent_ab: ["11", "11", "11", "11"]
  master_up_guard_db: 6.0

spectrum:
  enabled: true
  peak_floor_db: -80.0
  program_floor_db: -60.0

ui:
  knob_tags: dim                 # dim | off | bright: the E1-E4 tags on the screen

engine:
  url: ws://127.0.0.1:1234
  boot_gain_db: -40.0
  boot_ramp_s: 3.0

paths:
  state: /var/lib/k1000/state.json
  active_config: /var/lib/k1000/active.yml

phone:
  enabled: true
  max_clients: 4
  notch_gain_db: -6.0
  notch_q: 8.0
```

## 3. `profile`

| Key | Values | Default | Meaning |
|---|---|---|---|
| `profile` | `dev` or `m4` (case ignored) | none | **The pin.** With it, SETUP › Profile cannot switch the profile (hint `PROFILE PINNED IN k1000.yaml`) |

| `profile:` line | Profile that runs |
|---|---|
| `m4` or `dev` | That one, pinned |
| absent | The last one chosen on SETUP (saved in `state.json`), else `dev` |
| unknown name, or no readable `profile:` line in a file that does not parse | Alert `SETTINGS: …`; the last one running (saved in `state.json`), else `dev`. Never silently `dev` when a pin was meant |
| the file does not parse, but its `profile:` line reads | That profile, pinned, plus the alert `SETTINGS: …` |

**Gig rule: `profile: m4`.** If the M4 is missing, the screen shows NO DEVICE. Nothing falls back to
the HiFiBerry, which is not wired to the speakers.

Changing the profile changes the audio device: about a second of silence, then the 3 s ramp.
Bench only.

## 4. `profiles`

One section per profile. Every key is optional: a missing key keeps the built-in value.

| Key | Values | `dev` default | `m4` default |
|---|---|---|---|
| `capture_device`, `playback_device` | An ALSA name, by card **name**, never number: `hw:<card id>` | `hw:sndrpihifiberry` | `hw:M4` |
| `capture_channels`, `playback_channels` | A whole number, or `null` | 2, 2 | `null` = probe the device (assumed 4 when it cannot be probed) |
| `capture_format`, `playback_format` | `S16_LE`, `S24_3_LE`, `S24_4_LE`, `S32_LE`, `F32_LE`, `F64_LE`, or `null` | `S32_LE` | `null` = CamillaDSP picks (S32_LE first) |
| `input_map` | `[L, R]`: the capture channels (0-based) that carry the program | `[0, 1]` | `[2, 3]` = LINE IN 3 and 4 |
| `output_map` | One entry per physical output, in order: which logical channel it carries. `SubL`, `SubR`, `TopL`, `TopR` (or 0–3) | (not used: `dev` has an audition mixer) | `[TopL, TopR, SubL, SubR]` = out1 Top L, out2 Top R, out3 Sub L, out4 Sub R. The Sunday patch (D-012) feeds both subs from out3 (Mono subs ON makes out3 = out4 = the L+R sum); out4 is spare |
| `probe` | `true` / `false` | `false` (the HiFiBerry is never opened by the probe) | `true` |
| `target_level` | Frames, 64–1536 | 256 | 512 (D-010; the code default is 256) |

Details:

- **Probing** (`probe: true`) runs when k1000 starts and on a SETUP › Profile switch. It reads
  what the device accepts with `aplay`/`arecord --dump-hw-params`, or from
  `/proc/asound/<card>/stream0` when the engine holds the device. It sets the channel counts. A
  channel count you set that the device refuses is replaced by the device's maximum, with a
  warning. It never chooses a format, but a format you set that the device does not offer is
  dropped (CamillaDSP then picks), with a warning.
- **Pin the M4 channel counts** (`4` and `4`) once the bring-up has proven them
  ([`docs/05-m4-day.md`](../05-m4-day.md) step 5). A late-plugged M4 then always gets the proven
  config.
- **`input_map` outside the capture channels** falls back to the profile's own default when it
  fits (m4: `[2, 3]`), else `[0, 1]`. It never quietly picks the M4's front inputs 1–2, which are
  mic preamps.
- **Any `input_map` or `output_map` problem** (a bad entry, too many entries, a logical channel
  routed nowhere) raises the alert `k1000.yaml ROUTING FIXED UP (SEE LOG)`. The journal says what
  was fixed.
- **`target_level`** is the playback buffer CamillaDSP keeps, in frames (256 = 5.3 ms at 48 kHz).
  A USB interface may need more margin against dropouts: the shipped `m4` value is 512 (~10.7 ms),
  768 if a soak still shows an underrun. Values outside 64–1536 are
  clamped with a warning. It is an engine device setting, so it applies **at the next engine
  start** only, never at show time ([§9](#9-what-needs-a-restart)).
- **`dev` only:** the 4 logical outputs are folded onto the HiFiBerry's 2. SETUP › Audition (dev)
  picks `sum` (each side = top + sub at −6 dB each), `tops` or `subs`.

The fixed parts, not configurable: 48 kHz, chunk size 256, no rate adjustment, Main capped at 0 dB
(`volume_limit`), and fader ramps of 100 ms (Main and the group faders: master and balance answer
the hand, and the mute fades over 0.1 s). An engine still running an older config with another
ramp shows `DEVICES CHANGED (GUI)` until its next start.

## 5. `controls`

Which physical knob and button does what. Physical indices are the MCP23017's: encoders 0–3,
switches 0–7 (0–3 are the encoder pushes, 4–7 the buttons on the V4).

| Key | Values | Default | Meaning |
|---|---|---|---|
| `encoders` | 4 distinct numbers, 0–3 | `[0, 1, 2, 3]` | E1, E2, E3, E4 = these physical encoders |
| `pushes` | 4 numbers, 0–7 | `[0, 1, 2, 3]` | The push of E1…E4 = these switches |
| `buttons` | 4 numbers, 0–7 | `[4, 5, 6, 7]` | B1, B2, B3, B4 = these switches |
| `reverse` | 4 × `true`/`false` (or 0/1) | all `false` | Flip an encoder's direction |
| `steps_per_detent` | `1`, `2` or `4`, one number for all or a list of 4 (per **physical** encoder `enc0`…`enc3`) | `[4, 4, 4, 4]` | Contact transitions per click of the encoder |
| `detent_ab` | `"11"`, `"10"`, `"01"`, `"00"` or `"idle"`, one value or a list of 4 (per physical encoder) | `["11", "11", "11", "11"]` | The A/B contact levels when the knob rests on a click (`"11"` = both open; `"idle"` = whatever they read at start) |
| `master_up_guard_db` | 0 … 24 dB | 6 | The master-up guard: master may rise at most this much within any 2 s, from the knobs, the phone and the console alike. Further up-clicks are refused with `MASTER: SLOW DOWN`. Down is never limited. `0` = off |

- `pushes` and `buttons` together must name 8 different switches.
- Anything invalid in `encoders`, `pushes`, `buttons` or `reverse` (wrong length, a duplicate, a
  string, a float) silently falls back to the **default** for that key. Check the result on
  SETUP › Knob test. An invalid `steps_per_detent` or `detent_ab` keeps its default and logs
  `k1000.yaml controls.… ignored`. An unreadable `master_up_guard_db` means 6.
- **Find the map with SETUP › Knob test**: press every button and turn every knob, note the
  `sw`/`enc` index shown for each physical position, write them here, restart k1000. Until this is
  done, **MUTE may sit on another button**: a wrong `buttons` order moves the hold-B2 mute
  elsewhere (a hold on the button that is really B4 only says `RESET LIVE EQ: IN SETUP`, and
  nothing mutes). Label the buttons on the shell once the map is right.
- **`steps_per_detent` and `detent_ab`** describe the encoders themselves. The defaults suit the
  Zynthian V4's knobs. For another encoder, record a trace with `k1000-knobtrace.py capture
  --guided` ([chapter 8](08-development.md#the-knob-tools)): it prints the `controls:` lines to
  use. Background: [`docs/08-knob-precision.md`](../08-knob-precision.md).
- The screen draws E1 top-left, E2 bottom-left, E3 top-right, E4 bottom-right (confirmed on the
  first box). If the labels sit at the wrong corners on another shell, that is `KNOB_CORNERS` in
  `box/k1000/ui/input_map.py`, a code change.
- Acceleration is fixed in the code: only FREQ, the Lo-Cut frequency and master **down** speed up
  on a fast turn (up to ×4); master **up** never does.

### The knob tags: `ui.knob_tags`

Next to each knob's label (`MASTER`, `DELAY`, `ITEM`…) the screen can draw the knob's name, `E1`
to `E4` (D-011). One key, in its own `ui:` section:

| `ui.knob_tags` | Looks like | For |
|---|---|---|
| `dim` (default) | A small dark-grey `E1` before the label: secondary to it, legible up close | Most boxes |
| `off` | No tag; the label moves into the corner where the tag was | A sticker on the shell names the knobs: E1 top-left, E2 bottom-left, E3 top-right, E4 bottom-right. Put the sticker on first: the hints still name the knobs (`E3 TO CUT`, the PHONE page's `E1 turn`) |
| `bright` | The cyan `E1` of UI v1 as first shipped | A new operator learning the knobs |

```yaml
ui:
  knob_tags: off
```

- Two lines, the second indented. On one line it is `ui: {knob_tags: off}`: `ui: knob_tags: off`
  does not parse, and a file that does not parse loses every key but `profile:` (§1).
- Every page that names its knobs follows it: SHOW, ALIGN, SETUP. The Knob test keeps its own
  `E1`…`E4` roles (it is there to identify them), and so do the PHONE page's hints.
- A bare `off` works (YAML reads it as false); `on` or `true` means `dim`. Anything else is `dim`,
  with one line in the journal. Read at k1000's start: restart k1000 after a change.
- Pictures: [`show.png`](../ui/show.png) (dim), [`show_tags_off.png`](../ui/show_tags_off.png),
  [`show_tags_bright.png`](../ui/show_tags_bright.png), [`align_tags_off.png`](../ui/align_tags_off.png).

## 6. `engine` and `paths`

| Key | Values | Default | Meaning |
|---|---|---|---|
| `engine.url` | `ws://…` or `wss://…` | `ws://127.0.0.1:1234` | CamillaDSP's websocket (the engine unit listens there) |
| `engine.boot_gain_db` | −80 … −20 dB (clamped) | −40 | Where Main is parked, muted, before the ramp to the saved master. The engine itself always starts at −40 dB |
| `engine.boot_ramp_s` | 0.5 … 30 s (clamped) | 3 | Length of the ramp to the saved master. No step is ever larger than 6 dB |
| `paths.state` | a path | `/var/lib/k1000/state.json` | The saved state |
| `paths.active_config` | a path | `/var/lib/k1000/active.yml` | The engine config K1000 writes. **The engine unit reads this exact path**: change it only for tests |

## 7. `spectrum`

The program spectrum behind SHOW's EQ curve, and its PEAK pointer ([chapter 4 §3.1](04-usage.md#31-the-curve-and-the-spectrum)).
K1000 reads it from CamillaDSP (`GetSpectrum`, capture side, the profile's `input_map` channels) on
a second websocket of its own, about 10 times a second, only while SHOW is on screen and
SETUP › Spectrum is ON.

| Key | Values | Default | Meaning |
|---|---|---|---|
| `enabled` | `true` / `false` | `true` | `false` removes the spectrum entirely: no task, no second engine connection; SETUP › Spectrum reads `DISABLED`. The no-go switch if the spectrum misbehaves on your box. Only a real `false` turns it off |
| `peak_floor_db` | −140 … −20 dBFS | −80 | No PEAK unless the peak band reaches this level (the engine's scale: 0 = a full-scale sine) |
| `program_floor_db` | −140 … −20 dBFS | −60 | No PEAK unless the program's RMS (its two input channels, the last 0.5 s) reaches this level, with 3 dB of hysteresis. So no PEAK on silence or between tracks |

- **−140 turns a gate off.** A value outside −140 … −20, or not a number, falls back to the
  default with one warning line in the journal.
- With no PEAK, B4's notch helper sets a band up at its own frequency (`NO PEAK`).
- **SETUP › Spectrum** (ON/OFF) is not configuration: it is saved in `state.json` (`spectrum_on`)
  and only stops the polling and the picture. `enabled: false` here is the stronger switch.

## 8. `phone`

The phone page ([`docs/06-phone.md`](../06-phone.md)). The template shows the first four keys; the
others are optional.

| Key | Values | Default | Meaning |
|---|---|---|---|
| `enabled` | `true` / `false` | `true` | `false` = no phone servers at all, knobs only (and no `/tune` page). A phone no-go is this **and** `sudo /opt/k1000/net/setup-ap.sh --down` (the Wi-Fi off the air; it stays off across reboots and later `install.sh --ap` runs, until `setup-ap.sh --up`) |
| `max_clients` | 1–16 | 4 | Phones connected at once |
| `notch_gain_db` | −20 … 0 dB | −6 | "Notch here" on the phone: the new band's cut |
| `notch_q` | 0.3 … 16 | 8 | … and its Q |
| `master_up_db_per_s` | 0.5 … 40 | 6 | Fastest master rise, all phones together (with a burst of half that). Down is never limited. The box's master-up guard (`controls.master_up_guard_db`) applies on top |
| `rate_per_s`, `burst` | 1–200, 1–400 | 30, 40 | Messages per second per phone (a mute is never limited) |
| `address` | an IPv4 address | `10.42.0.1` | The access point address: where the servers listen and the URL host |
| `bind` | an IPv4 address | = `address` | Listen on another address |
| `hosts` | a list of names | `[address, k1000.home.arpa]` | Host names the page accepts |
| `https_port` | 0–65535 | 443 | The installable page (needs a certificate). 0 = off |
| `http_port` | 0–65535 | 80 | First-time page, CA download, captive portal. 0 = off |
| `plain_port` | 0–65535 | 8080 | The page over plain HTTP. 0 = off |
| `plain_app` | `fallback`, `always`, `off` | `fallback` | `fallback` = the plain page only when no certificate is installed |
| `tls_dir` | a path | `/etc/k1000/tls` | `k1000.crt`, `k1000.key`, `k1000-ca.crt` |
| `ssid`, `iface`, `band` | text | `K1000`, `wlan0`, (from `ap.info`) | Labels for SETUP › Phone. The access point itself is set by `setup-ap.sh` |
| `psk_file`, `ap_info_file` | paths | `/etc/k1000/ap.psk`, `/etc/k1000/ap.info` | Written by `setup-ap.sh` |
| `state_file` | a path | `/var/lib/k1000/phone.json` | SETUP › Phone on/off, kept across restarts |
| `extra_origins` | a list | none | More web origins the page accepts (development) |

Invalid values fall back to their defaults with one warning line in the journal.

The access point itself (band, channel, portal, SSH, backend, interface, country, password) is not
in this file: it is `box/net/setup-ap.sh` and its options ([`docs/06-phone.md`](../06-phone.md),
[chapter 2](02-installation.md#6-installsh-reference)). Its country defaults to FR; set
`K1000_AP_COUNTRY=<code>` when running it elsewhere. It never shares its subnet: while another
interface holds an address in 10.42.0.0/24 (a laptop on the Ethernet cable sharing its connection
with NetworkManager's default), the access point stays down. Share on another subnet, e.g.
10.43.0.1/24.

Laptop access (SETUP › Laptop access, [chapter 4 §5.1](04-usage.md#51-laptop-access-and-the-tune-page))
has no key here either: its ports (22, 5005), subnet and 2 h limit are the root helper's
(`box/net/k1000-laptop-access.sh`). The `/tune` page needs the phone servers (`enabled: true`).

## 9. What needs a restart

Every key above is read at k1000's start. Restarting k1000 never stops the audio. Some changes also
touch the engine:

| Change | Do | What you hear |
|---|---|---|
| `controls`, `engine.*`, `spectrum.*`, `ui.*`, `phone.*` | `sudo systemctl restart k1000` | Nothing. `boot_gain_db`/`boot_ramp_s` apply at the next fresh engine start. The screen and knobs pause for a few seconds; an open Laptop access closes |
| `profile`, or a profile's device or channel counts | Restart k1000 | K1000 switches the engine to the new device: about 1 s of silence, then the 3 s ramp. **Bench only** |
| `input_map`, `output_map` | Restart k1000 | K1000 rebuilds the engine's pipeline with Main muted around it. Possible click. **Bench only** |
| `target_level`, a format | Restart k1000, then `sudo systemctl restart camilladsp` | k1000 rewrites `active.yml`; the screen may say `DEVICES CHANGED (GUI)` until the engine restarts. The engine restart is about 1 s of silence, then the ramp |
| The phone certificate | Restart k1000 | Nothing; phones reconnect by themselves |

## 10. The state files

All in `/var/lib/k1000/`, owned by the `k1000` user, written by the k1000 service.

| File | What | When it is written |
|---|---|---|
| `state.json` | **The sound**: levels, mute, EQ, delays, trims, profile ([§11](#11-statejson-fields)) | Atomically (temp file, fsync, rename), only when the content changed. 1 s after the last edit, at most 5 s after the first. **Edits toward silence** (a mute, the master turned down) are saved at once |
| `state.json.bad` | A copy of an unreadable `state.json`, kept for a post-mortem | When k1000 finds one at start |
| `active.yml` | The CamillaDSP config K1000 generated from the state. **The engine boots from it** | With `state.json` |
| `active.yml.prev` | The previous `active.yml`: the config the engine had booted from | At k1000's start, only when the `active.yml` it regenerates differs from the one on disk (for example after an update, a `k1000.yaml` change or a hand edit of `state.json`). Not on an ordinary restart, and never again until k1000's next start |
| `phone.json` | SETUP › Phone on/off | When toggled |
| `tune-undo.json` | The system EQ the last `/tune` Apply replaced (its one-level undo) | At each Apply; removed by Undo |

A failed write is retried after 2, 5 and 10 s and raises `CANNOT SAVE STATE` on the screen.

**At start**, k1000 reads them like this:

| `state.json` | `active.yml` | Start state |
|---|---|---|
| valid | — | It (clamped; files from older builds are upgraded) |
| torn or unreadable | a K1000 config | Rebuilt from `active.yml` (the tune: system EQ, trims, delays, polarity); master and balance at defaults; **muted**. Hint `STATE REBUILT · CHECK MASTER` |
| missing | a K1000 config | Defaults, **muted**. Hint `NO SAVED STATE · CHECK MASTER` |
| missing or torn | none | Defaults (a first boot: unmuted, ramps to −20 dB) |

**Editing `state.json` by hand** needs both services stopped. While k1000 runs it overwrites your
edit; and if the engine is still playing when k1000 starts again, k1000 keeps the engine's values
and saves them over the file. Only an engine that starts fresh gets the saved state pushed into it:

```sh
sudo systemctl stop k1000 camilladsp
sudo nano /var/lib/k1000/state.json
sudo systemctl start k1000          # starts the engine too; or power-cycle
```

Silence while stopped, then the 3 s ramp: between shows only. Every value is clamped to its range
on load.

## 11. `state.json` fields

Logical outputs, wherever four values appear: 0 Sub L, 1 Sub R, 2 Top L, 3 Top R.

| Field | Range | Default | Set from |
|---|---|---|---|
| `master_db` | −80 … 0 dB, 0.5 dB steps | −20 | E1 on SHOW and ALIGN, phone |
| `muted` | bool | false | Hold B2, phone |
| `balance_db` | −12 … +12 dB, + = subs up | 0 | ALIGN E2, phone. Subs get +b/2, tops −b/2 |
| `input_trim_db` | −24 … +12 dB | 0 | SETUP |
| `mono_subs` | bool | false | SETUP (rebuilds the pipeline: two pushes). Both subs get (L+R) at −6 dB each |
| `sub_inverted`, `top_inverted` | bool | false | SETUP |
| `sub_delay_ms` | 0 … 50 ms | 0 | ALIGN E3 (unlocked): DELAY clockwise of 0, `SUBS LATER` |
| `top_delay_ms` | 0 … 50 ms | 0 | ALIGN E3 (unlocked): DELAY anticlockwise of 0, `TOPS LATER` |
| `lowcut_on` | bool | **true** | SETUP › Lo-Cut (two pushes), phone (with a confirm) |
| `lowcut_hz` | 20 … 300 Hz, 2 Hz steps (push: 1 Hz) | 100 | ALIGN E4 (unlocked), phone. Tops only, 12 dB/oct |
| `sub_lowpass_on` | bool | false | SETUP |
| `sub_lowpass_hz` | 40 … 250 Hz | 120 | SETUP. Subs only, 12 dB/oct |
| `sys_peq` | 8 bands: freq 20 Hz–20 kHz, gain −20 … +10 dB, Q 0.3–16 | flat at 63, 125, 250, 500, 1k, 2k, 4k, 8k Hz, Q 1 | The laptop: the `/tune` page or CamillaGUI ([chapter 5](05-tuning.md#7-system-eq)) |
| `live_peq` | 4 bands: freq 20 Hz–20 kHz, gain −20 … **+6** dB, Q 0.3–16 | flat at 250, 1k, 2.5k, 5k Hz, Q 4 | SHOW (E2–E4, B4), phone |
| `live_lowshelf` | freq 30–1000 Hz, gain −12 … +6 dB, Q 0.4–1.5 | 120 Hz, flat, Q 0.707 | SHOW, phone |
| `live_highshelf` | freq 1–16 kHz, gain −12 … +6 dB, Q 0.4–1.5 | 8 kHz, flat, Q 0.707 | SHOW, phone |
| `out_trim_db` | 4 × −24 … +6 dB | 0 | SETUP (Trim Sub L … Top R) |
| `profile` | `dev`, `m4` | `dev` | SETUP › Profile (unless pinned) |
| `dev_audition` | `sum`, `tops`, `subs` | `sum` | SETUP (profile `dev` only) |
| `spectrum_on` | bool | true | SETUP › Spectrum. The screen only: it never touches the engine config |

"Off" for the Lo-Cut and the sub low-pass is a frequency move (10 Hz and 20 kHz), never a pipeline
change. Delays are applied in whole samples at 48 kHz.

**The two delays are one knob** (D-011). ALIGN E3 shows `sub_delay_ms − top_delay_ms`: 0.1 ms a
click, one sample (1/48 ms) with the push, stopping one click on `ALIGNED` (0) before crossing to
the other side. A knob move sets the late side and zeroes the other. A file with both set (written
by hand, by CamillaGUI or by a build before D-011) keeps both until the first E3 change (the
wake-up click aside), which moves the net value one step, writes it to the late side and zeroes
the other: sub 2.0 / top 0.5 shows `1.50 ms`, `SUBS LATER · 0.51 m`, and one click clockwise
stores sub 1.6, top 0. A `lowcut_hz` between the 2 Hz steps (e.g. 112.25) also stays as it is until its first
click, which lands on the next step in the turn's direction (114 up, 112 down). Reset live EQ puts
the four bells and both shelves back to the defaults above.

## 12. Other files in `/etc/k1000`

| File | What | Who writes it |
|---|---|---|
| `tls/k1000.crt`, `tls/k1000.key`, `tls/k1000-ca.crt` | The phone page's certificate, its key and the CA's **public** certificate. root:k1000 0640 | `install.sh` with `K1000_TLS_DIR` |
| `ap.psk` | The K1000 Wi-Fi password, shown only on the box screen. root:k1000 0640 | `setup-ap.sh`, once |
| `ap.info` | The access point's choices: SSID, band, channel, interface, backend, portal, SSH, and `state=down` after a `--down` (kept until `--up`) | `setup-ap.sh` |
| `hostapd.conf` (0600), `ap.env`, `ap-dnsmasq.conf`, `ap-fence.nft` | Generated by `setup-ap.sh` | `setup-ap.sh` |

None of these is in the repository. Back them up off the box ([chapter 7](07-maintenance.md)).
