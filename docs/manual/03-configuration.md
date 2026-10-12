# 3 — Configuration

Everything the box reads at start lives in **`/etc/k1000/k1000.yaml`**. The sound itself (levels,
EQ, delays) is not configuration: it is the **state**, saved by K1000 in `/var/lib/k1000/` as you
work ([§9](#9-the-state-files)).

## 1. The file

- Read **once, when the k1000 service starts**. After any edit:
  `sudo systemctl restart k1000` (between shows: the audio keeps playing, the knobs and the screen
  pause for a few seconds).
- Copied from the repository's `box/etc/k1000.yaml` at the first install, then **never overwritten**
  by `install.sh`.
- **Missing** = built-in defaults, the same values as the template.
- **Lenient.** A bad value never stops the box: it falls back to its default (or is clamped into
  its range). For `profile`, `profiles` and `phone` the journal says so (`journalctl -u k1000`).
  `controls`, `engine` and `paths` fall back **silently**: check the knobs on SETUP › Knob test.
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
    target_level: 256

controls:
  encoders: [0, 1, 2, 3]
  pushes: [0, 1, 2, 3]
  buttons: [4, 5, 6, 7]
  reverse: [false, false, false, false]

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
| `output_map` | One entry per physical output, in order: which logical channel it carries. `SubL`, `SubR`, `TopL`, `TopR` (or 0–3) | (not used: `dev` has an audition mixer) | `[TopL, TopR, SubL, SubR]` = out1 Top L, out2 Top R, out3 Sub L, out4 Sub R |
| `probe` | `true` / `false` | `false` (the HiFiBerry is never opened by the probe) | `true` |
| `target_level` | Frames, 64–1536 | 256 | 256 |

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
  A USB interface may need more margin against dropouts (512 or 768). Values outside 64–1536 are
  clamped with a warning. It is an engine device setting, so it applies **at the next engine
  start** only, never at show time ([§8](#8-what-needs-a-restart)).
- **`dev` only:** the 4 logical outputs are folded onto the HiFiBerry's 2. SETUP › Audition (dev)
  picks `sum` (each side = top + sub at −6 dB each), `tops` or `subs`.

The fixed parts, not configurable: 48 kHz, chunk size 256, no rate adjustment, Main capped at 0 dB
(`volume_limit`), and today fader ramps of 300 ms (D-009 plans shorter ones with UI v1).
<!-- TODO(after UI v1): re-check VOLUME_RAMP_MS in box/k1000/dspconfig.py. -->


## 5. `controls`

Which physical knob and button does what. Physical indices are the MCP23017's: encoders 0–3,
switches 0–7 (0–3 are the encoder pushes, 4–7 the buttons on the V4).

| Key | Values | Default | Meaning |
|---|---|---|---|
| `encoders` | 4 distinct numbers, 0–3 | `[0, 1, 2, 3]` | E1, E2, E3, E4 = these physical encoders |
| `pushes` | 4 numbers, 0–7 | `[0, 1, 2, 3]` | The push of E1…E4 = these switches |
| `buttons` | 4 numbers, 0–7 | `[4, 5, 6, 7]` | B1, B2, B3, B4 = these switches |
| `reverse` | 4 × `true`/`false` (or 0/1) | all `false` | Flip an encoder's direction |

- `pushes` and `buttons` together must name 8 different switches.
- Anything invalid (wrong length, a duplicate, a string, a float) silently falls back to the
  **default** for that key. Check the result on SETUP › Knob test.
- **Find the map with SETUP › Knob test**: press every button and turn every knob, note the
  `sw`/`enc` index shown for each physical position, write them here, restart k1000. Until this is
  done, **hold B2 (mute) and hold B4 (reset live EQ) may be swapped**: they are the same gesture.
- The screen draws E1 top-left, E2 bottom-left, E3 top-right, E4 bottom-right (confirmed on the
  first box). If the labels sit at the wrong corners on another shell, that is `KNOB_CORNERS` in
  `box/k1000/ui/input_map.py`, a code change.
- The knob fix in [`docs/08-knob-precision.md`](../08-knob-precision.md) (Patch A) adds per-encoder
  `steps_per_detent` and `detent_ab` keys here. They do not exist until that patch lands.
  <!-- TODO(after UI v1): if Patch A landed, document steps_per_detent / detent_ab (values,
  defaults, how to find them with the knob trace) in the table above. -->


## 6. `engine` and `paths`

| Key | Values | Default | Meaning |
|---|---|---|---|
| `engine.url` | `ws://…` or `wss://…` | `ws://127.0.0.1:1234` | CamillaDSP's websocket (the engine unit listens there) |
| `engine.boot_gain_db` | −80 … −20 dB (clamped) | −40 | Where Main is parked, muted, before the ramp to the saved master. The engine itself always starts at −40 dB |
| `engine.boot_ramp_s` | 0.5 … 30 s (clamped) | 3 | Length of the ramp to the saved master. No step is ever larger than 6 dB |
| `paths.state` | a path | `/var/lib/k1000/state.json` | The saved state |
| `paths.active_config` | a path | `/var/lib/k1000/active.yml` | The engine config K1000 writes. **The engine unit reads this exact path**: change it only for tests |

## 7. `phone`

The phone page ([`docs/06-phone.md`](../06-phone.md)). The template shows the first four keys; the
others are optional.

<!-- TODO(after UI v1): re-check this table and §2's example against box/etc/k1000.yaml and
box/k1000/web/server.py (PhoneConfig) once the phone wiring is committed. -->

| Key | Values | Default | Meaning |
|---|---|---|---|
| `enabled` | `true` / `false` | `true` | `false` = no phone servers at all, knobs only. A phone no-go is this **and** `sudo /opt/k1000/net/setup-ap.sh --down` (the Wi-Fi off the air) |
| `max_clients` | 1–16 | 4 | Phones connected at once |
| `notch_gain_db` | −20 … 0 dB | −6 | "Notch here" on the phone: the new band's cut |
| `notch_q` | 0.3 … 16 | 8 | … and its Q |
| `master_up_db_per_s` | 0.5 … 40 | 6 | Fastest master rise, all phones together. Down is never limited |
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
`K1000_AP_COUNTRY=<code>` when running it elsewhere.

### Coming with UI v1

The live UI v1 ([`docs/07-live-ui-v1.md`](../07-live-ui-v1.md), decision D-009) plans these. They
are **not in the code yet**; this table is updated when they land.

<!-- TODO(after UI v1): move what landed (RTA kill switch key, rta_source, master-up handling)
into §7/§10 with the real key names and ranges, and drop this table. -->

| Planned | Where | What |
|---|---|---|
| A master-up guard (D-009) | No `k1000.yaml` key planned | Against a wrong-knob sweep on the master. [`docs/07`](../07-live-ui-v1.md) §2.2 implements it as no acceleration on master up ([`docs/08`](../08-knob-precision.md) Patch B), a code change |
| An RTA kill switch | a `ui:` key (docs/07 §8 suggests `ui: {rta: false}`) | Turns the spectrum off for a show |
| `rta_source` | `state.json` (SETUP › RTA source) | PGM / MIC / OUT / OFF |

## 8. What needs a restart

Every key above is read at k1000's start. Restarting k1000 never stops the audio. Some changes also
touch the engine:

| Change | Do | What you hear |
|---|---|---|
| `controls`, `engine.*`, `phone.*` | `sudo systemctl restart k1000` | Nothing. `boot_gain_db`/`boot_ramp_s` apply at the next fresh engine start |
| `profile`, or a profile's device or channel counts | Restart k1000 | K1000 switches the engine to the new device: about 1 s of silence, then the 3 s ramp. **Bench only** |
| `input_map`, `output_map` | Restart k1000 | K1000 rebuilds the engine's pipeline with Main muted around it. Possible click. **Bench only** |
| `target_level`, a format | Restart k1000, then `sudo systemctl restart camilladsp` | k1000 rewrites `active.yml`; the screen may say `ENGINE DEVICES CHANGED OUTSIDE K1000` until the engine restarts. The engine restart is about 1 s of silence, then the ramp |
| The phone certificate | Restart k1000 | Nothing; phones reconnect by themselves |

## 9. The state files

All in `/var/lib/k1000/`, owned by the `k1000` user, written by the k1000 service.

| File | What | When it is written |
|---|---|---|
| `state.json` | **The sound**: levels, mute, EQ, delays, trims, profile ([§10](#10-statejson-fields)) | Atomically (temp file, fsync, rename), only when the content changed. 1 s after the last edit, at most 5 s after the first. **Edits toward silence** (a mute, the master turned down) are saved at once |
| `state.json.bad` | A copy of an unreadable `state.json`, kept for a post-mortem | When k1000 finds one at start |
| `active.yml` | The CamillaDSP config K1000 generated from the state. **The engine boots from it** | With `state.json` |
| `active.yml.prev` | The previous `active.yml`: the config the engine had booted from | At k1000's start, only when the `active.yml` it regenerates differs from the one on disk (for example after an update, a `k1000.yaml` change or a hand edit of `state.json`). Not on an ordinary restart, and never again until k1000's next start |
| `phone.json` | SETUP › Phone on/off | When toggled |

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

## 10. `state.json` fields

Logical outputs, wherever four values appear: 0 Sub L, 1 Sub R, 2 Top L, 3 Top R.

| Field | Range | Default | Set from (v0 UI) |
|---|---|---|---|
| `master_db` | −80 … 0 dB, 0.5 dB steps | −20 | MAIN E1 |
| `muted` | bool | false | Hold B2 |
| `balance_db` | −12 … +12 dB, + = subs up | 0 | MAIN E2. Subs get +b/2, tops −b/2 |
| `input_trim_db` | −24 … +12 dB | 0 | SETUP |
| `mono_subs` | bool | false | SETUP (rebuilds the pipeline: two pushes). Both subs get (L+R) at −6 dB each |
| `sub_inverted`, `top_inverted` | bool | false | SETUP |
| `sub_delay_ms` | 0 … 50 ms, 0.1 ms steps, push = 1 sample (1/48 ms) | 0 | MAIN E3 |
| `top_delay_ms` | same | 0 | SETUP |
| `lowcut_on` | bool | **true** | MAIN B3 |
| `lowcut_hz` | 20 … 300 Hz, 1/12-octave steps | 100 | MAIN E4. Tops only, 12 dB/oct |
| `sub_lowpass_on` | bool | false | SETUP |
| `sub_lowpass_hz` | 40 … 250 Hz | 120 | SETUP. Subs only, 12 dB/oct |
| `sys_peq` | 8 bands: freq 20 Hz–20 kHz, gain −20 … +10 dB, Q 0.3–16 | flat at 63, 125, 250, 500, 1k, 2k, 4k, 8k Hz, Q 1 | CamillaGUI on a laptop ([chapter 5](05-tuning.md#7-system-eq)) |
| `live_peq` | 4 bands: freq 20 Hz–20 kHz, gain −20 … **+6** dB, Q 0.3–16 | flat at 250, 1k, 2.5k, 5k Hz, Q 4 | LIVE page, phone |
| `live_lowshelf` | freq 30–1000 Hz, gain −12 … +6 dB, Q 0.4–1.5 | 120 Hz, flat, Q 0.707 | LIVE page, phone |
| `live_highshelf` | freq 1–16 kHz, gain −12 … +6 dB, Q 0.4–1.5 | 8 kHz, flat, Q 0.707 | LIVE page, phone |
| `out_trim_db` | 4 × −24 … +6 dB | 0 | SETUP (Trim Sub L … Top R) |
| `profile` | `dev`, `m4` | `dev` | SETUP › Profile (unless pinned) |
| `dev_audition` | `sum`, `tops`, `subs` | `sum` | SETUP (profile `dev` only) |

"Off" for the Lo-Cut and the sub low-pass is a frequency move (10 Hz and 20 kHz), never a pipeline
change. Delays are applied in whole samples at 48 kHz. Reset live EQ puts the four bells and both
shelves back to the defaults above.

## 11. Other files in `/etc/k1000`

| File | What | Who writes it |
|---|---|---|
| `tls/k1000.crt`, `tls/k1000.key`, `tls/k1000-ca.crt` | The phone page's certificate, its key and the CA's **public** certificate. root:k1000 0640 | `install.sh` with `K1000_TLS_DIR` |
| `ap.psk` | The K1000 Wi-Fi password, shown only on the box screen. root:k1000 0640 | `setup-ap.sh`, once |
| `ap.info` | The access point's choices: SSID, band, channel, interface, backend, portal, SSH | `setup-ap.sh` |
| `hostapd.conf` (0600), `ap.env`, `ap-dnsmasq.conf`, `ap-fence.nft` | Generated by `setup-ap.sh` | `setup-ap.sh` |

None of these is in the repository. Back them up off the box ([chapter 7](07-maintenance.md)).
