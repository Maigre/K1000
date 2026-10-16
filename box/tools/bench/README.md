# Bench tools (M4 day, 2026-10-16)

Small scripts used to bring the MOTU M4 up and measure the box (results: `docs/05-m4-day.md` §13).
They run **on the box**, next to the live engine, and talk to it over its own websockets: the
CamillaDSP one (`ws://127.0.0.1:1234`) and the phone protocol (`wss://10.42.0.1/v2/ws`). Nothing
here edits K1000's code or settings files; `monosubs.py`, `v2nudge.py` and `v2mute.py` change the
sound the way a phone or CamillaGUI would (speakers off, or know what you are doing).

Copies also sit in `/home/k1000/m4-day/` on the box. After the next deploy they are in
`/opt/k1000/tools/bench/`. Python 3 with numpy and websockets (both on the box already).

| Script | What it does |
|---|---|
| `cdspstat.py` | Engine state, capture rate, buffer level, load, faders, 2 s peaks and RMS per channel. Read-only |
| `levels.py [label] [--ext]` | Plays the 1 kHz test tone on the HiFiBerry (unless `--ext`) and prints capture and playback RMS |
| `m4loop.py OUT [--src 2] [--ret 3] [--ext]` | The loop test: source into one M4 line input (capture 2 = LINE IN 3), output OUT looped into the other (3 = LINE IN 4). Prints the analog loop gain, the engine's digital gain, the M4's analog offset, the 1/3-octave response and the **latency** (comb of source + return in the engine's spectrum, ±0.005 ms). Plays a sine then pink noise on the HiFiBerry unless `--ext` |
| `m4loop_sim.py <dir of m4loop.py>` | Offline check of the latency finder on simulated engine spectra (runs anywhere with numpy) |
| `sweep.py [Hz ...]` | Stepped-sine response through the loop (capture 3 minus capture 2), exact at each frequency |
| `monosubs.py on\|off` | Mono L+R on or off through the engine config; k1000 adopts it like a CamillaGUI edit |
| `v2nudge.py FIELD DETENTS` | Nudges a field like a phone (e.g. `balance_db 12` = +6 dB) |
| `v2mute.py on\|off` | Mute or unmute like a phone (the unmute carries the current mute sequence) |
| `pgmprobe.py [s]` | Subscribes to the RESPONSE program windows like a phone and summarises them. Read-only |

Loop wiring rule: the return always goes into the **other** side's line input (source on LINE IN 3 →
return into LINE IN 4 from OUT1 or OUT3, and the mirror for OUT2/OUT4), with Mono L+R off: a return
into the same side feeds the output back into itself through the DSP.

## From a laptop at the rig

The box's own Wi-Fi is the only way in (no house network). Join the K1000 Wi-Fi (the PHONE page
shows the password while laptop access is open), then on the box SETUP › PHONE › **Laptop access**,
push twice (2 hours). Then `ssh k1000@10.42.0.1` with the key the box knows (`docs/06-phone.md` §11).
State files to read before anything else: `/var/lib/k1000/state.json` (debounced: the engine's
faders are the live truth, `cdspstat.py`), `/etc/k1000/k1000.yaml`, `journalctl -u k1000 -u camilladsp
-u k1000-usbwatch`.
